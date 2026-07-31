"""Offline tests for BuzzClient's HTTP bridge error surfacing."""

from __future__ import annotations

import uuid

import buzzkit
import httpx
import pytest
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
