"""Offline tests for BuzzClient: HTTP bridge errors, auth handshake, huddle start."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import buzzkit
import httpx
import pytest
import websockets
from buzzkit.client import BuzzClient


async def test_post_event_surfaces_relay_error_body(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"error": "invalid: edit target event not found"})

    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kw: real(transport=httpx.MockTransport(handler), timeout=1),
    )
    nsec, _, _ = buzzkit.generate_keypair()
    bz = BuzzClient("wss://relay.example", nsec)
    ev = buzzkit.build_message_event(nsec, str(uuid.uuid4()), "x")
    with pytest.raises(RuntimeError, match="edit target event not found"):
        await bz.post_event(ev)


# ── WebSocket auth handshake (fake relay) ────────────────────────────────────


@contextlib.asynccontextmanager
async def serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


async def _challenge_and_read_auth(ws) -> dict:
    await ws.send(json.dumps(["AUTH", "challenge-1"]))
    return json.loads(await ws.recv())[1]


async def test_connect_completes_on_accepted_auth():
    async def handler(ws):
        auth = await _challenge_and_read_auth(ws)
        await ws.send(json.dumps(["OK", auth["id"], True, ""]))
        await ws.wait_closed()

    nsec, _, _ = buzzkit.generate_keypair()
    async with serve(handler) as url:
        bz = BuzzClient(url, nsec)
        await asyncio.wait_for(bz.connect(), 2)
        await bz.close()


async def test_connect_fails_fast_on_rejected_auth():
    async def handler(ws):
        auth = await _challenge_and_read_auth(ws)
        await ws.send(json.dumps(["OK", auth["id"], False, "restricted: not a relay member"]))
        await ws.wait_closed()

    nsec, _, _ = buzzkit.generate_keypair()
    async with serve(handler) as url:
        bz = BuzzClient(url, nsec)
        # Well under the 20 s auth timeout: the rejection itself ends connect().
        with pytest.raises(RuntimeError, match="not a relay member"):
            await asyncio.wait_for(bz.connect(), 2)
        assert bz._ws is None


async def test_connect_fails_fast_when_closed_before_auth():
    async def handler(ws):
        await _challenge_and_read_auth(ws)
        await ws.close(code=1008, reason="community deleted")

    nsec, _, _ = buzzkit.generate_keypair()
    async with serve(handler) as url:
        bz = BuzzClient(url, nsec)
        with pytest.raises(RuntimeError, match="closed before NIP-42 auth"):
            await asyncio.wait_for(bz.connect(), 2)
        assert bz.close_code == 1008
