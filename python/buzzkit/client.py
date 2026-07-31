"""Async client for a Buzz relay: HTTP bridge + authenticated WebSocket.

HTTP methods (:meth:`BuzzClient.post_event`, :meth:`send_message`,
:meth:`query`, :meth:`claim_invite`, …) are one-shot and need no connection.
WebSocket methods (:meth:`subscribe`, :meth:`publish`) require :meth:`connect`
first — or use the client as an async context manager::

    async with BuzzClient(relay_url, nsec) as bz:
        async for event in bz.subscribe_channel(channel_id):
            ...

Reconnection is intentionally left to the caller (e.g. RoomKit's SourceProvider
already owns reconnect/health); on a dropped socket, active subscriptions receive
a final ``closed`` and their iterators stop.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import websockets

from . import _native
from .agents import verify_agent_profile

logger = logging.getLogger("buzzkit.client")

_AUTH_TIMEOUT = 20.0
_OK_TIMEOUT = 20.0
_MAX_FRAME = 1 << 20  # 1 MiB — matches the relay's max frame size


def _checked(r: httpx.Response) -> httpx.Response:
    """Raise a readable error carrying the relay's response body on 4xx/5xx.

    The bridge explains rejections in the body (e.g. ``{"error": "invalid:
    edit target event not found"}``); a bare ``raise_for_status`` hides it.
    """
    if r.status_code >= 400:
        raise RuntimeError(f"HTTP bridge error {r.status_code} on {r.url.path}: {r.text}")
    return r


def _to_http(relay_url: str) -> str:
    u = relay_url.rstrip("/")
    if u.startswith("wss://"):
        return "https://" + u[len("wss://") :]
    if u.startswith("ws://"):
        return "http://" + u[len("ws://") :]
    return u


def _to_ws(relay_url: str) -> str:
    u = relay_url.rstrip("/")
    if u.startswith("https://"):
        return "wss://" + u[len("https://") :]
    if u.startswith("http://"):
        return "ws://" + u[len("http://") :]
    return u


class BuzzClient:
    """A Buzz relay client, keyed by a single Nostr secret (agent identity)."""

    def __init__(self, relay_url: str, secret: str, *, auth_tag: str | None = None) -> None:
        self.relay_url = relay_url
        self._http = _to_http(relay_url)
        self._ws_url = _to_ws(relay_url)
        self._secret = secret
        self._auth_tag = auth_tag  # NIP-OA owner attestation (AUTH + profile)
        self.npub, self.pubkey_hex = _native.pubkey_from_secret(secret)
        #: WebSocket close code from the last disconnect (``None`` while
        #: connected or never connected). 1012 means the relay is restarting
        #: (graceful drain): reconnect with backoff and dedupe replayed
        #: events by id after resubscribing.
        self.close_code: int | None = None
        self._ws: Any = None
        self._reader: asyncio.Task | None = None
        self._authed = asyncio.Event()
        self._auth_event_id: str | None = None
        self._subs: dict[str, asyncio.Queue] = {}
        self._pending_ok: dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------ HTTP

    def _nip98(self, method: str, url: str, body: bytes | None) -> str:
        return _native.sign_nip98(self._secret, method, url, body)

    async def post_event(self, event_json: str) -> dict:
        """POST a signed event to the HTTP bridge (non-ephemeral kinds only).

        The bridge acknowledges before the event is fully indexed: a revision
        (edit/delete/reaction) sent immediately after its target can be
        rejected with "target event not found". Retrying is the caller's call.
        """
        url = f"{self._http}/events"
        body = event_json.encode("utf-8")
        headers = {
            "Authorization": self._nip98("POST", url, body),
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=20.0) as c:
            return _checked(await c.post(url, content=body, headers=headers)).json()

    async def send_message(
        self,
        channel_id: str,
        content: str,
        mentions: list[str] | None = None,
        *,
        reply_to: str | None = None,
        reply_root: str | None = None,
    ) -> dict:
        """Build + post a channel chat message (kind 9).

        ``reply_to`` threads the message under that event id; for a nested
        reply also pass ``reply_root`` (the thread's root event id).
        """
        ev = _native.build_message_event(
            self._secret, channel_id, content, mentions, reply_to, reply_root
        )
        return await self.post_event(ev)

    async def react(self, target_event_id: str, emoji: str = "👍") -> dict:
        """React to an event (kind 7)."""
        ev = _native.build_reaction_event(self._secret, target_event_id, emoji)
        return await self.post_event(ev)

    async def remove_reaction(self, reaction_event_id: str) -> dict:
        """Delete one of our own reactions (kind 5) by its event id."""
        ev = _native.build_remove_reaction_event(self._secret, reaction_event_id)
        return await self.post_event(ev)

    async def edit_message(self, channel_id: str, target_event_id: str, content: str) -> dict:
        """Replace one of our own messages (kind 40003)."""
        ev = _native.build_edit_event(self._secret, channel_id, target_event_id, content)
        return await self.post_event(ev)

    async def delete_message(
        self, channel_id: str, target_event_id: str, *, reason: str | None = None
    ) -> dict:
        """Delete a message (kind 9005 tombstone). Management kind — needs
        :meth:`connect` first; ``reason`` is shown room-facing."""
        ev = _native.build_delete_message_event(self._secret, channel_id, target_event_id, reason)
        return await self.publish(ev)

    async def set_topic(self, channel_id: str, topic: str) -> dict:
        """Set a channel's topic (kind 9002). Needs :meth:`connect` first."""
        ev = _native.build_set_topic_event(self._secret, channel_id, topic)
        return await self.publish(ev)

    async def leave_channel(self, channel_id: str) -> dict:
        """Leave a channel (kind 9022). Needs :meth:`connect` first."""
        ev = _native.build_leave_event(self._secret, channel_id)
        return await self.publish(ev)

    async def set_profile(
        self,
        display_name: str,
        *,
        about: str | None = None,
        picture: str | None = None,
        auth_tag: str | None = None,
    ) -> dict:
        """Publish this identity's profile (kind 0) so it shows a name in Buzz.

        ``auth_tag`` defaults to the client's NIP-OA owner attestation, so a
        client constructed with one automatically shows as "managed by
        <owner>" in the Buzz desktop (which reads the tag from kind 0).
        """
        ev = _native.build_profile_event(
            self._secret,
            display_name=display_name,
            name=display_name,
            about=about,
            picture=picture,
            auth_tag=auth_tag if auth_tag is not None else self._auth_tag,
        )
        return await self.post_event(ev)

    async def join_channel(self, channel_id: str) -> dict:
        """Self-add as a bot member of a channel (NIP-29 kind 9000).

        Published over the WebSocket — NIP-29 management events are not accepted
        on the HTTP bridge — so :meth:`connect` must be called first. Required
        for the identity's messages to reach other channel members and for it to
        appear in mention autocomplete.
        """
        ev = _native.build_join_channel_event(self._secret, channel_id)
        return await self.publish(ev)

    async def start_huddle(
        self, parent_channel_id: str, *, name: str | None = None, ttl: int = 3600
    ) -> str:
        """Start a huddle in a channel; returns the ephemeral huddle channel id.

        Creates a private ephemeral channel (kind 9007, over the WebSocket —
        :meth:`connect` first) and posts the kind-48100 announcement to the
        parent channel. Join the audio with
        ``HuddleClient(..., huddle_id, parent_channel_id=parent_channel_id)``.
        """
        huddle_id = str(uuid.uuid4())
        create_ev = _native.build_create_channel_event(
            self._secret,
            huddle_id,
            name or f"huddle-{huddle_id[:8]}",
            visibility="private",
            channel_type="stream",
            ttl=ttl,
        )
        result = await self.publish(create_ev)
        if not result["accepted"]:
            raise RuntimeError(f"huddle channel rejected: {result['message']}")
        started_ev = _native.build_huddle_started_event(self._secret, parent_channel_id, huddle_id)
        result = await self.publish(started_ev)
        if not result["accepted"]:
            raise RuntimeError(f"huddle announcement rejected: {result['message']}")
        return huddle_id

    async def publish_presence(self, status: str = "online") -> dict:
        """Announce presence (kind 20001, ephemeral) over the WebSocket.

        Cadence is the caller's job. Relays at buzz >= v0.5.x hold presence
        for a 180 s TTL and expect a heartbeat every 60 s; older relays used
        90 s / 30 s. When the relay version is unknown, 30 s is safe on both.
        """
        ev = _native.build_presence_event(self._secret, status)
        return await self.publish(ev)

    async def set_status(self, text: str, *, emoji: str | None = None) -> dict:
        """Publish a NIP-38 user status (kind 30315). Empty text clears it."""
        ev = _native.build_user_status_event(self._secret, text, emoji)
        return await self.post_event(ev)

    async def query(self, filters: list[dict]) -> list[dict]:
        """Run a NIP-01 REQ over the HTTP bridge; returns a list of events."""
        url = f"{self._http}/query"
        body = json.dumps(filters).encode("utf-8")
        headers = {
            "Authorization": self._nip98("POST", url, body),
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=20.0) as c:
            data = _checked(await c.post(url, content=body, headers=headers)).json()
            return data if isinstance(data, list) else []

    async def list_channels(self) -> list[dict]:
        """List channel metadata (kind 39000) as ``{channel_id, name, event}``."""
        out = []
        for ev in await self.query([{"kinds": [39000], "limit": 500}]):
            tags = {t[0]: t[1] for t in ev.get("tags", []) if len(t) >= 2}
            out.append({"channel_id": tags.get("d"), "name": tags.get("name"), "event": ev})
        return out

    async def resolve_agent(self, name: str, owner_pubkey: str | None = None) -> list[dict]:
        """Resolve agents named ``name`` from an owner's managed-agent records.

        Mirrors upstream ``buzz users get --name X --owner …``: only kind-30177
        records authored by the owner are consulted (exact name match,
        case-insensitive), and each candidate's kind-0 profile must carry
        exactly one valid NIP-OA auth tag from that owner — cryptographically
        verified — to count as owned.

        ``owner_pubkey`` defaults to this client's own owner: the attesting
        pubkey of its NIP-OA auth tag, or its own pubkey without one. Returns
        one dict per candidate: ``{pubkey, verification, profile}`` plus
        ``owner_pubkey`` when ``verification == "verified"``.
        """
        owner = owner_pubkey or self.owner_pubkey_hex
        records = await self.query(
            [{"kinds": [_native.KIND_MANAGED_AGENT], "authors": [owner], "limit": 1000}]
        )
        pubkeys: set[str] = set()
        for ev in records:
            try:
                record_name = json.loads(ev.get("content", ""))["name"]
            except (json.JSONDecodeError, TypeError, KeyError):
                continue
            if not isinstance(record_name, str) or record_name.lower() != name.lower():
                continue
            d_tag = next((t[1] for t in ev.get("tags", []) if len(t) >= 2 and t[0] == "d"), "")
            if d_tag:
                pubkeys.add(d_tag)
        if not pubkeys:
            return []
        profiles = await self.query([{"kinds": [0], "authors": sorted(pubkeys), "limit": 1000}])
        by_author = {ev["pubkey"]: ev for ev in profiles if "pubkey" in ev}
        out = []
        for pk in sorted(pubkeys):
            profile = by_author.get(pk)
            if profile is None:
                verification = "missing_profile"
                content: dict = {}
            else:
                verification = verify_agent_profile(profile, owner)
                try:
                    content = json.loads(profile.get("content", "") or "{}")
                except json.JSONDecodeError:
                    content = {}
            entry = {"pubkey": pk, "verification": verification, "profile": content}
            if verification == "verified":
                entry["owner_pubkey"] = owner
            out.append(entry)
        return out

    @property
    def owner_pubkey_hex(self) -> str:
        """This identity's effective owner: its auth-tag attester, else itself."""
        if self._auth_tag is not None:
            return json.loads(self._auth_tag)[1]
        return self.pubkey_hex

    async def claim_invite(self, code_or_url: str) -> dict:
        """Redeem a relay invite: accept the join-policy (if any), then claim.

        Accepts a full ``.../invite/<code>`` URL or a bare code.
        """
        code = code_or_url.rsplit("/invite/", 1)[-1].strip().strip("/")
        async with httpx.AsyncClient(timeout=20.0) as c:
            policy = (await c.get(f"{self._http}/api/join-policy")).json().get("policy")
            receipt = None
            if policy:
                rp = await c.post(
                    f"{self._http}/api/invites/accept-policy",
                    json={
                        "code": code,
                        "policy_version": policy["version"],
                        "age_confirmed": True,
                    },
                )
                receipt = _checked(rp).json()["receipt"]
            payload: dict = {"code": code}
            if receipt:
                payload["policy_receipt"] = receipt
            url = f"{self._http}/api/invites/claim"
            body = json.dumps(payload).encode("utf-8")
            headers = {
                "Authorization": self._nip98("POST", url, body),
                "Content-Type": "application/json",
            }
            rc = await c.post(url, content=body, headers=headers)
            return _checked(rc).json()

    # ------------------------------------------------------------- WebSocket

    async def __aenter__(self) -> BuzzClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def connect(self) -> None:
        """Open the WebSocket and complete the NIP-42 auth handshake."""
        self._authed.clear()
        self.close_code = None
        self._ws = await websockets.connect(self._ws_url, max_size=_MAX_FRAME)
        self._reader = asyncio.create_task(self._read_loop())
        await asyncio.wait_for(self._authed.wait(), timeout=_AUTH_TIMEOUT)

    async def _read_loop(self) -> None:
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not msg:
                    continue
                typ = msg[0]
                if typ == "EVENT":
                    q = self._subs.get(msg[1])
                    if q is not None:
                        await q.put(("event", msg[2]))
                elif typ == "OK":
                    self._on_ok(msg)
                elif typ == "AUTH":
                    await self._on_auth_challenge(msg[1])
                elif typ == "EOSE":
                    q = self._subs.get(msg[1])
                    if q is not None:
                        await q.put(("eose", None))
                elif typ == "CLOSED":
                    q = self._subs.get(msg[1])
                    if q is not None:
                        await q.put(("closed", msg[2] if len(msg) > 2 else ""))
                elif typ == "NOTICE":
                    logger.warning("relay NOTICE: %s", msg[1] if len(msg) > 1 else "")
        except websockets.ConnectionClosed as e:
            close = e.rcvd or e.sent
            self.close_code = close.code if close else None
            logger.info("buzz websocket closed (code %s)", self.close_code)
        finally:
            for q in self._subs.values():
                q.put_nowait(("closed", "connection lost"))

    async def _on_auth_challenge(self, challenge: str) -> None:
        auth_ev = json.loads(
            _native.build_auth_event(self._secret, challenge, self._ws_url, self._auth_tag)
        )
        self._auth_event_id = auth_ev["id"]
        await self._ws.send(json.dumps(["AUTH", auth_ev]))

    def _on_ok(self, msg: list) -> None:
        event_id = msg[1]
        accepted = bool(msg[2]) if len(msg) > 2 else False
        message = msg[3] if len(msg) > 3 else ""
        if event_id == self._auth_event_id:
            if accepted:
                self._authed.set()
            else:
                logger.error("NIP-42 auth rejected: %s", message)
            return
        fut = self._pending_ok.pop(event_id, None)
        if fut is not None and not fut.done():
            fut.set_result((accepted, message))

    async def publish(self, event_json: str) -> dict:
        """Publish a signed event over the WebSocket and await the relay OK.

        Use this for ephemeral kinds (20000–29999) which the HTTP bridge rejects.
        """
        if self._ws is None:
            raise RuntimeError("call connect() before publish()")
        ev = json.loads(event_json)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_ok[ev["id"]] = fut
        await self._ws.send(json.dumps(["EVENT", ev]))
        accepted, message = await asyncio.wait_for(fut, timeout=_OK_TIMEOUT)
        return {"accepted": accepted, "event_id": ev["id"], "message": message}

    async def subscribe(
        self, filters: list[dict], *, sub_id: str = "sub", close_on_eose: bool = False
    ) -> AsyncIterator[dict]:
        """Subscribe with NIP-01 filters and async-iterate matching events."""
        if self._ws is None:
            raise RuntimeError("call connect() before subscribe()")
        q: asyncio.Queue = asyncio.Queue()
        self._subs[sub_id] = q
        await self._ws.send(json.dumps(["REQ", sub_id, *filters]))
        try:
            while True:
                kind, payload = await q.get()
                if kind == "event":
                    yield payload
                elif kind == "eose":
                    if close_on_eose:
                        break
                elif kind == "closed":
                    break
        finally:
            self._subs.pop(sub_id, None)
            # the socket may already be gone
            with contextlib.suppress(Exception):
                await self._ws.send(json.dumps(["CLOSE", sub_id]))

    async def subscribe_channel(
        self, channel_id: str, *, kinds: list[int] | None = None, **kw: Any
    ) -> AsyncIterator[dict]:
        """Subscribe to a channel's messages (default kind 9) via its h-tag."""
        f = {"kinds": kinds or [_native.KIND_STREAM_MESSAGE], "#h": [channel_id]}
        async for ev in self.subscribe([f], **kw):
            yield ev

    async def close(self) -> None:
        """Cancel the reader task and close the WebSocket."""
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
