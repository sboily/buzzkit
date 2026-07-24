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
import threading
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

# Outbound backlog cap (frames). A realtime voice provider front-loads a whole
# utterance — it generates a 2 s response in ~0.5 s — and the huddle wire plays
# at real time (50 frames/s), so the backlog MUST hold a full response and pace
# it out; truncating it drops most of the speech. This cap therefore holds
# ~10 s of audio and only guards against pathological unbounded growth (a stuck
# consumer), never trims a normal response. Between turns the queue drains to
# empty, so latency does not accumulate across turns.
_MAX_QUEUE_FRAMES = 500


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
        stream_silence: bool = True,
        paced: bool = True,
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
        # A real microphone never stops sending: between utterances the
        # receivers' jitter buffers must keep seeing the media timeline
        # advance in real time, or their catch-up corrections (accelerate/
        # expand) audibly distort the next utterance. When idle, the sender
        # encodes silence through the same encoder — Opus DTX shrinks it to
        # 1-2 byte comfort packets after a short hangover.
        #
        # ``paced`` (default) runs a built-in wall-clock pacer: good enough
        # for simple senders (echo bots, tests). A realtime AI provider should
        # instead pace with prebuffer + jitter headroom to survive the sending
        # process's own scheduling jitter (see roomkit's OutboundAudioPacer);
        # such callers set ``paced=False`` and drive send_pcm on their own
        # clock, and this client just relays each frame to the wire ASAP.
        self._paced = paced
        self._stream_silence = stream_silence if paced else False
        #: Pacing health counters: sent, stalls (>100 ms between sends),
        #: burst_sends (<5 ms), max_gap_ms.
        self.sender_stats: dict[str, float] = {
            "sent": 0,
            "stalls": 0,
            "burst_sends": 0,
            "dropped_late": 0,
            "max_gap_ms": 0.0,
        }
        # All WebSocket I/O runs on a private event loop in a dedicated
        # thread: audio pacing must not inherit the caller's loop stalls
        # (a realtime-provider websocket burst can block an asyncio loop
        # for 50-100 ms, which receivers hear as dropped audio). Public
        # async methods bridge onto this loop; events() re-publishes onto
        # the caller's loop.
        self._io_loop: asyncio.AbstractEventLoop | None = None
        self._io_thread: threading.Thread | None = None
        self._recv_pump: asyncio.Task | None = None
        self._subscribers: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        # Events that arrive between connect() and the first events() call are
        # held here (bounded) so early roster/audio isn't lost; the first
        # subscriber drains it. Guarded by _sub_lock against the I/O thread.
        self._backlog: deque[HuddleEvent | None] | None = deque(maxlen=512)
        self._sub_lock = threading.Lock()

    async def __aenter__(self) -> HuddleClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.leave()

    # ------------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        """Open the audio WebSocket and complete the challenge/auth/joined handshake.

        The connection (and all subsequent audio I/O) lives on the client's
        private I/O thread; this coroutine only bridges onto it.
        """
        self._start_io_thread()
        assert self._io_loop is not None
        try:
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(self._connect_impl(), self._io_loop)
            )
        except BaseException:
            self._stop_io_thread()
            raise

    async def _connect_impl(self) -> None:
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
        send_loop = self._send_loop if self._paced else self._relay_loop
        self._sender = asyncio.create_task(send_loop())
        self._recv_pump = asyncio.create_task(self._recv_loop())

    def _start_io_thread(self) -> None:
        if self._io_loop is not None:
            return
        loop = asyncio.new_event_loop()

        def run() -> None:
            asyncio.set_event_loop(loop)
            loop.run_forever()
            loop.close()

        thread = threading.Thread(target=run, name="buzzkit-huddle-io", daemon=True)
        thread.start()
        self._io_loop = loop
        self._io_thread = thread

    def _stop_io_thread(self) -> None:
        loop, self._io_loop = self._io_loop, None
        thread, self._io_thread = self._io_thread, None
        if loop is not None:
            loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)

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
        if self._io_loop is not None:
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(self._teardown_impl(leave=True), self._io_loop)
            )
        self._stop_io_thread()

    async def close(self) -> None:
        """Close the socket and stop the paced sender (no leave message)."""
        if self._io_loop is not None:
            await asyncio.wrap_future(
                asyncio.run_coroutine_threadsafe(self._teardown_impl(leave=False), self._io_loop)
            )
        self._stop_io_thread()

    async def _teardown_impl(self, *, leave: bool) -> None:
        if leave and self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps({"type": "leave"}))
        for task in (self._sender, self._recv_pump):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self._sender = None
        self._recv_pump = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None

    # --------------------------------------------------------------- outbound

    def send_pcm(self, pcm: bytes) -> None:
        """Queue s16le mono 48 kHz PCM for paced sending.

        Encodes immediately into 20 ms wire frames; trailing samples short of
        a frame stay buffered until more PCM arrives or :meth:`flush_pcm`.

        The first frames of an utterance are sent as a small burst instead of
        strictly paced: receivers' jitter buffers use the cushion to ride out
        sender-side timing jitter (an asyncio loop is no audio clock), which
        they would otherwise patch audibly with expand/PLC. A live microphone
        cannot pre-buffer; the sender paces frames out one per 20 ms so the
        receiver's jitter buffer sees a smooth, real-time stream.
        """
        for frame in self._encoder.encode(pcm):
            self._out.append(frame)
        if self._out:
            self._wake_sender()

    def flush_pcm(self) -> None:
        """Zero-pad and queue the buffered partial frame (end of utterance)."""
        frame = self._encoder.flush()
        if frame is not None:
            self._out.append(frame)
            self._wake_sender()

    def _wake_sender(self) -> None:
        """Thread-safe wake of the paced sender on the I/O loop."""
        if self._io_loop is not None:
            self._io_loop.call_soon_threadsafe(self._out_ready.set)

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

    async def _relay_loop(self) -> None:
        """Unpaced send: relay queued frames to the wire as fast as they come.

        Used when ``paced=False`` — an external pacer (e.g. roomkit's
        OutboundAudioPacer) owns the wall-clock timing and silence-fill, so
        this loop must not add pacing of its own; it just forwards frames in
        order the instant they are queued.
        """
        assert self._ws is not None
        ws = self._ws
        stats = self.sender_stats
        try:
            while True:
                if not self._out:
                    self._out_ready.clear()
                    await self._out_ready.wait()
                    continue
                await ws.send(self._out.popleft())
                stats["sent"] += 1
        except websockets.ConnectionClosed:
            logger.info("huddle relay loop: connection closed")

    async def _send_loop(self) -> None:
        """Emit exactly one wire frame per 20 ms of wall clock — never a burst.

        ``next_at`` is the sole authority on *when* a frame goes out. After a
        send the clock advances by one frame; if we've fallen behind real time
        (an event-loop stall under GIL contention), the clock resyncs to *now +
        one frame* rather than staying in the past. That guarantees the next
        iteration actually sleeps, so a recovered stall resumes smooth pacing
        instead of flushing its backlog as a burst. A receiver's jitter buffer
        copes far better with a single missing frame (one 20 ms concealment)
        than with a burst (which it reads as an overrun and corrects with
        audible time-compression). Backlog latency is bounded by
        ``_MAX_QUEUE_FRAMES`` (oldest dropped past the cap).

        Between utterances (or whenever the queue is empty) ``stream_silence``
        encodes silence through the same encoder so seq/ts stay contiguous and
        advance in real time; Opus DTX keeps those packets tiny.
        """
        assert self._ws is not None
        ws = self._ws
        loop = asyncio.get_running_loop()
        silence_pcm = b"\x00\x00" * _native.HUDDLE_FRAME_SAMPLES
        stats = self.sender_stats
        # `next_at` advances by exactly one frame each tick (from the schedule,
        # not from the actual send time) so the average rate is exactly real
        # time — 50 frames/s. On recovery from an event-loop stall we do NOT
        # flush the backlog fast to catch up (a burst reads as an overrun at
        # the receiver and triggers audible time-compression); instead we DROP
        # the stale frames we couldn't send in time and realign the clock. So
        # the receiver sees a smooth stream with at most a short concealed gap.
        next_at = loop.time()
        last_send: float | None = None
        try:
            while True:
                delay = next_at - loop.time()
                if delay > 0:
                    await asyncio.sleep(delay)

                # Fell behind (GIL/loop stall): drop the frames whose slots
                # already passed and realign, rather than bursting them out.
                behind = int((loop.time() - next_at) / _FRAME_SECONDS)
                if behind >= 1:
                    dropped = min(behind, len(self._out))
                    for _ in range(dropped):
                        self._out.popleft()
                    stats["dropped_late"] += dropped
                    next_at += behind * _FRAME_SECONDS
                # Also bound standing latency if audio arrives faster than we
                # drain it (queue keeps growing without a stall).
                while len(self._out) > _MAX_QUEUE_FRAMES:
                    self._out.popleft()
                    stats["dropped_late"] += 1

                if self._out:
                    frame = self._out.popleft()
                elif self._stream_silence:
                    encoded = self._encoder.encode(silence_pcm)
                    # One full frame of input can carry over buffered partial
                    # samples, so 1..=2 frames can come out; queue any extra.
                    frame = encoded[0] if encoded else None
                    self._out.extend(encoded[1:])
                else:
                    self._out_ready.clear()
                    # Idle: wait for audio, but wake at least once per frame so
                    # the clock never drifts far while the queue is empty.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._out_ready.wait(), _FRAME_SECONDS)
                    next_at = loop.time()
                    continue

                if frame is not None:
                    await ws.send(frame)
                    now = loop.time()
                    stats["sent"] += 1
                    if last_send is not None:
                        gap = now - last_send
                        stats["max_gap_ms"] = max(stats["max_gap_ms"], gap * 1000)
                        if gap > 0.1:
                            stats["stalls"] += 1
                        elif gap < 0.005:
                            stats["burst_sends"] += 1
                    last_send = now
                next_at += _FRAME_SECONDS
        except websockets.ConnectionClosed:
            logger.info("huddle send loop: connection closed")

    # ---------------------------------------------------------------- inbound

    async def events(self) -> AsyncIterator[HuddleEvent]:
        """Iterate decoded audio and roster changes until the huddle ends.

        Frames are read and decoded on the client's private I/O thread and
        re-published onto this coroutine's loop. The iterator stops when the
        relay closes the socket (huddle ended, kicked, or relay restart);
        reconnecting is the caller's decision.
        """
        if self._io_loop is None:
            raise RuntimeError("call connect() before events()")
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()
        sub = (loop, queue)
        with self._sub_lock:
            backlog, self._backlog = self._backlog, None
            for pending in backlog or ():
                queue.put_nowait(pending)
            self._subscribers.append(sub)
        try:
            while True:
                event = await queue.get()
                if event is None:  # connection closed
                    return
                yield event
        finally:
            with self._sub_lock, contextlib.suppress(ValueError):
                self._subscribers.remove(sub)

    async def _recv_loop(self) -> None:
        """Read/decode on the I/O loop; publish to every events() subscriber."""
        assert self._ws is not None
        try:
            async for raw in self._ws:
                if isinstance(raw, bytes):
                    event = self._on_audio(raw)
                    if event is not None:
                        self._publish(event)
                    continue
                for event in self._on_control(_parse_json(raw)):
                    self._publish(event)
        except websockets.ConnectionClosed:
            logger.info("huddle websocket closed")
        finally:
            self._publish(None)

    def _publish(self, event: HuddleEvent | None) -> None:
        with self._sub_lock:
            if not self._subscribers:
                if self._backlog is not None:
                    self._backlog.append(event)
                return
            subs = list(self._subscribers)
        for loop, queue in subs:
            loop.call_soon_threadsafe(queue.put_nowait, event)

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
