"""Async client for a Buzz huddle audio WebSocket (Opus voice).

A huddle is an ephemeral Buzz channel with a dedicated audio endpoint,
``wss://<relay>/huddle/{channel_id}/audio``. The handshake is::

    relay → {"type": "challenge", "challenge": ...}
    client → {"type": "auth", "event": <NIP-42 kind 22242>,
              "parent_channel_id": ..., "protocol_version": 2}
    relay → {"type": "joined", "pubkey": ..., "peer_index": N, "peers": [...]}

then binary v2 wire frames flow both ways (Opus 48 kHz mono, 20 ms; see
``buzzkit._native.HuddleEncoder``/``HuddleDecoder``). Outbound audio is paced
at one frame per 20 ms — the relay's per-peer fan-out channels are bounded and
drop on overflow, so bursting a long utterance would lose audio at receivers.

Typical use::

    huddle = HuddleClient(relay_url, nsec, channel_id, parent_channel_id=parent)
    await huddle.connect()
    huddle.send_pcm(pcm_s16le_48k_mono)          # queued + paced
    async for ev in huddle.events():
        if isinstance(ev, HuddleAudio):
            ...                                   # ev.pcm, ev.pubkey
    await huddle.leave()

Huddles are discovered by subscribing to kind 48100 (huddle started) on the
parent channel; the event's JSON content carries ``ephemeral_channel_id``.
Passing ``parent_channel_id`` lets a member of the parent channel join the
ephemeral huddle without an explicit membership grant (relay auto-add).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections import deque
from collections.abc import AsyncIterator
from dataclasses import dataclass

import websockets

from . import _native
from .client import _to_ws

logger = logging.getLogger("buzzkit.huddle")

_HANDSHAKE_TIMEOUT = 10.0
_MAX_FRAME = 1 << 20
_FRAME_SECONDS = _native.HUDDLE_FRAME_SAMPLES / _native.HUDDLE_SAMPLE_RATE  # 0.02


@dataclass(frozen=True)
class HuddleAudio:
    """One decoded 20 ms audio frame from a remote peer."""

    peer_index: int
    pubkey: str | None
    pcm: bytes
    """s16le mono 48 kHz samples."""
    seq: int
    ts_48k: int
    level_dbov: int
    is_dtx: bool


@dataclass(frozen=True)
class HuddlePeerJoined:
    pubkey: str
    peer_index: int


@dataclass(frozen=True)
class HuddlePeerLeft:
    pubkey: str
    peer_index: int


HuddleEvent = HuddleAudio | HuddlePeerJoined | HuddlePeerLeft


class HuddleError(Exception):
    """Relay rejected the huddle handshake or connection."""


class HuddleClient:
    """One authenticated connection to a huddle's audio room.

    PCM in/out is s16le mono 48 kHz; Opus and the wire framing are handled by
    the Rust extension. Reconnection is the caller's concern (as with
    :class:`~buzzkit.client.BuzzClient`).
    """

    def __init__(
        self,
        relay_url: str,
        secret: str,
        channel_id: str,
        *,
        parent_channel_id: str | None = None,
        auth_tag: str | None = None,
        bitrate: int = 32_000,
        dtx: bool = True,
    ) -> None:
        self.relay_url = relay_url
        self.channel_id = channel_id
        self.parent_channel_id = parent_channel_id
        self._ws_base = _to_ws(relay_url)
        self._secret = secret
        self._auth_tag = auth_tag
        self.npub, self.pubkey_hex = _native.pubkey_from_secret(secret)
        self._encoder = _native.HuddleEncoder(bitrate=bitrate, dtx=dtx)
        self._decoder = _native.HuddleDecoder()
        self._ws: websockets.ClientConnection | None = None
        self.peer_index: int | None = None
        #: peer_index → pubkey for everyone currently in the room (incl. self).
        self.peers: dict[int, str] = {}
        self._out: deque[bytes] = deque()
        self._out_ready = asyncio.Event()
        self._sender: asyncio.Task | None = None

    async def __aenter__(self) -> HuddleClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.leave()

    # ------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        """Open the audio WebSocket and complete the challenge/auth/joined handshake."""
        url = f"{self._ws_base}/huddle/{self.channel_id}/audio"
        self._ws = await websockets.connect(url, max_size=_MAX_FRAME)
        try:
            async with asyncio.timeout(_HANDSHAKE_TIMEOUT):
                challenge = await self._await_challenge()
                await self._send_auth(challenge)
                await self._await_joined()
        except BaseException:
            await self._ws.close()
            self._ws = None
            raise
        self._sender = asyncio.create_task(self._send_loop())

    async def _await_challenge(self) -> str:
        assert self._ws is not None
        async for raw in self._ws:
            if isinstance(raw, bytes):
                continue
            msg = _parse_json(raw)
            if msg.get("type") == "challenge":
                challenge = msg.get("challenge")
                if not isinstance(challenge, str):
                    raise HuddleError("malformed challenge from relay")
                return challenge
            if msg.get("type") == "error":
                raise HuddleError(str(msg.get("message", "relay error")))
        raise HuddleError("connection closed before challenge")

    async def _send_auth(self, challenge: str) -> None:
        assert self._ws is not None
        # The NIP-42 relay tag carries the relay's base URL (same expectation
        # as the main relay door), not the /huddle/... endpoint URL.
        event = json.loads(
            _native.build_auth_event(self._secret, challenge, self._ws_base, self._auth_tag)
        )
        await self._ws.send(
            json.dumps(
                {
                    "type": "auth",
                    "event": event,
                    "parent_channel_id": self.parent_channel_id,
                    "protocol_version": _native.HUDDLE_PROTOCOL_VERSION,
                }
            )
        )

    async def _await_joined(self) -> None:
        assert self._ws is not None
        async for raw in self._ws:
            if isinstance(raw, bytes):
                continue
            msg = _parse_json(raw)
            typ = msg.get("type")
            if typ == "joined" and msg.get("pubkey") == self.pubkey_hex:
                index = msg.get("peer_index")
                self.peer_index = index if isinstance(index, int) else None
                self.peers = _parse_peers(msg.get("peers"))
                return
            if typ == "error":
                raise HuddleError(str(msg.get("message", "relay error")))
        raise HuddleError("connection closed before joined")

    async def leave(self) -> None:
        """Tell the relay we are leaving, then close the socket."""
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "leave"}))
        await self.close()

    async def close(self) -> None:
        """Close the socket and stop the paced sender (no leave message)."""
        if self._sender is not None:
            self._sender.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._sender
            self._sender = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    # --------------------------------------------------------------- outbound

    def send_pcm(self, pcm: bytes) -> None:
        """Queue s16le mono 48 kHz PCM for paced sending.

        Encodes immediately into 20 ms wire frames; trailing samples short of
        a frame stay buffered until more PCM arrives or :meth:`flush_pcm`.
        """
        for frame in self._encoder.encode(pcm):
            self._out.append(frame)
        if self._out:
            self._out_ready.set()

    def flush_pcm(self) -> None:
        """Zero-pad and queue the buffered partial frame (end of utterance)."""
        frame = self._encoder.flush()
        if frame is not None:
            self._out.append(frame)
            self._out_ready.set()

    def clear_queue(self) -> int:
        """Drop all queued outbound audio (barge-in). Returns frames dropped."""
        self._encoder.discard()
        dropped = len(self._out)
        self._out.clear()
        return dropped

    @property
    def queued_frames(self) -> int:
        """Outbound frames waiting to be sent (20 ms each)."""
        return len(self._out)

    async def _send_loop(self) -> None:
        """Send one wire frame per 20 ms; idle resets the cadence."""
        assert self._ws is not None
        ws = self._ws
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        try:
            while True:
                if not self._out:
                    self._out_ready.clear()
                    await self._out_ready.wait()
                    next_at = loop.time()
                frame = self._out.popleft()
                delay = next_at - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)
                await ws.send(frame)
                next_at += _FRAME_SECONDS
        except websockets.ConnectionClosed:
            logger.info("huddle send loop: connection closed")

    # ---------------------------------------------------------------- inbound

    async def events(self) -> AsyncIterator[HuddleEvent]:
        """Iterate decoded audio and roster changes until the huddle ends.

        The iterator is the connection's only reader — run exactly one. It
        stops when the relay closes the socket (huddle ended, kicked, or
        relay restart); reconnecting is the caller's decision.
        """
        if self._ws is None:
            raise RuntimeError("call connect() before events()")
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    event = self._on_audio(raw)
                    if event is not None:
                        yield event
                    continue
                for event in self._on_control(_parse_json(raw)):
                    yield event
        except websockets.ConnectionClosed:
            logger.info("huddle websocket closed")

    def _on_audio(self, frame: bytes) -> HuddleAudio | None:
        try:
            peer_index, seq, ts_48k, level, is_dtx, pcm = self._decoder.decode(frame)
        except ValueError as exc:
            logger.warning("dropping malformed audio frame: %s", exc)
            return None
        return HuddleAudio(
            peer_index=peer_index,
            pubkey=self.peers.get(peer_index),
            pcm=pcm,
            seq=seq,
            ts_48k=ts_48k,
            level_dbov=level,
            is_dtx=is_dtx,
        )

    def _on_control(self, msg: dict) -> list[HuddleEvent]:
        typ = msg.get("type")
        if typ == "joined":
            pubkey = msg.get("pubkey")
            index = msg.get("peer_index")
            if not isinstance(pubkey, str) or not isinstance(index, int):
                return []
            self.peers[index] = pubkey
            if pubkey == self.pubkey_hex:
                return []
            return [HuddlePeerJoined(pubkey=pubkey, peer_index=index)]
        if typ == "left":
            pubkey = msg.get("pubkey")
            index = msg.get("peer_index")
            if not isinstance(pubkey, str) or not isinstance(index, int):
                return []
            self.peers.pop(index, None)
            # Indexes are recycled — a future peer must not inherit this
            # stream's decoder state.
            self._decoder.remove_peer(index)
            return [HuddlePeerLeft(pubkey=pubkey, peer_index=index)]
        if typ == "roster":
            # Cross-pod resync: authoritative snapshot replaces the roster.
            fresh = _parse_peers(msg.get("peers"))
            events: list[HuddleEvent] = [
                HuddlePeerLeft(pubkey=pk, peer_index=idx)
                for idx, pk in self.peers.items()
                if idx not in fresh and pk != self.pubkey_hex
            ]
            events.extend(
                HuddlePeerJoined(pubkey=pk, peer_index=idx)
                for idx, pk in fresh.items()
                if idx not in self.peers and pk != self.pubkey_hex
            )
            for event in events:
                if isinstance(event, HuddlePeerLeft):
                    self._decoder.remove_peer(event.peer_index)
            self.peers = fresh
            return events
        if typ == "error":
            logger.warning("huddle relay error: %s", msg.get("message"))
        return []


def _parse_json(raw: str | bytes) -> dict:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return msg if isinstance(msg, dict) else {}


def _parse_peers(peers: object) -> dict[int, str]:
    out: dict[int, str] = {}
    if isinstance(peers, list):
        for p in peers:
            if (
                isinstance(p, dict)
                and isinstance(p.get("peer_index"), int)
                and isinstance(p.get("pubkey"), str)
            ):
                out[p["peer_index"]] = p["pubkey"]
    return out
