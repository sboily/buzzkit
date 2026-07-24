"""HuddleClient tests against an in-process fake huddle relay.

The fake server speaks the relay's handshake (challenge → auth → joined) and
each test scripts what happens after. No real relay or network needed.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import time

import buzzkit
import pytest
import websockets
from buzzkit import HuddleAudio, HuddleClient, HuddleError, HuddlePeerJoined, HuddlePeerLeft

PEER_PUBKEY = "ab" * 32  # fake remote peer (index 0)
CHANNEL_ID = "2a29484c-aac0-4ceb-93f4-9cee196348cb"
PARENT_ID = "3b39484c-aac0-4ceb-93f4-9cee19634000"


def sine_pcm(frames: int) -> bytes:
    """s16le mono 48 kHz sine covering `frames` 20 ms frames."""
    n = frames * buzzkit.HUDDLE_FRAME_SAMPLES
    return b"".join(
        int(8000 * math.sin(2 * math.pi * 440 * i / 48000)).to_bytes(2, "little", signed=True)
        for i in range(n)
    )


async def relay_handshake(ws) -> dict:
    """Server side of challenge → auth → joined. Returns the auth message."""
    await ws.send(json.dumps({"type": "challenge", "challenge": "c-123"}))
    while True:
        auth = json.loads(await ws.recv())
        if auth.get("type") == "auth":
            break
    pubkey = auth["event"]["pubkey"]
    await ws.send(
        json.dumps(
            {
                "type": "joined",
                "pubkey": pubkey,
                "peer_index": 1,
                "peers": [
                    {"pubkey": PEER_PUBKEY, "peer_index": 0},
                    {"pubkey": pubkey, "peer_index": 1},
                ],
            }
        )
    )
    return auth


@contextlib.asynccontextmanager
async def serve(handler):
    server = await websockets.serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        yield f"ws://127.0.0.1:{port}"
    finally:
        server.close()
        await server.wait_closed()


def make_client(url: str, **kw) -> HuddleClient:
    """Silence streaming is off by default so frame-exact assertions hold."""
    nsec, _, _ = buzzkit.generate_keypair()
    kw.setdefault("stream_silence", False)
    return HuddleClient(url, nsec, CHANNEL_ID, parent_channel_id=PARENT_ID, **kw)


class TestHandshake:
    async def test_connect_negotiates_v2_and_builds_roster(self):
        seen: dict = {}

        async def handler(ws):
            seen.update(await relay_handshake(ws))
            await ws.wait_closed()

        async with serve(handler) as url:
            client = make_client(url)
            await client.connect()
            assert client.peer_index == 1
            assert client.peers[0] == PEER_PUBKEY
            assert client.peers[1] == client.pubkey_hex
            await client.close()

        assert seen["protocol_version"] == buzzkit.HUDDLE_PROTOCOL_VERSION
        assert seen["parent_channel_id"] == PARENT_ID
        event = seen["event"]
        assert event["kind"] == buzzkit.KIND_AUTH
        assert ["challenge", "c-123"] in event["tags"]
        assert buzzkit.verify_event(json.dumps(event))

    async def test_relay_error_raises(self):
        async def handler(ws):
            await ws.send(json.dumps({"type": "challenge", "challenge": "c-123"}))
            await ws.recv()
            await ws.send(json.dumps({"type": "error", "message": "not a member"}))
            await ws.wait_closed()

        async with serve(handler) as url:
            client = make_client(url)
            with pytest.raises(HuddleError, match="not a member"):
                await client.connect()


class TestInbound:
    async def test_audio_and_roster_events(self):
        # A second encoder plays the remote peer: its wire frames get the
        # relay's 1-byte peer_index prefix before being sent to the client.
        remote = buzzkit.HuddleEncoder()
        wire_frames = [b"\x00" + f for f in remote.encode(sine_pcm(2))]
        new_peer = "cd" * 32

        async def handler(ws):
            await relay_handshake(ws)
            for frame in wire_frames:
                await ws.send(frame)
            await ws.send(json.dumps({"type": "joined", "pubkey": new_peer, "peer_index": 2}))
            await ws.send(json.dumps({"type": "left", "pubkey": new_peer, "peer_index": 2}))

        async with serve(handler) as url:
            client = make_client(url)
            await client.connect()
            events = [ev async for ev in client.events()]
            await client.close()

        audio = [ev for ev in events if isinstance(ev, HuddleAudio)]
        assert len(audio) == 2
        assert all(ev.pubkey == PEER_PUBKEY and ev.peer_index == 0 for ev in audio)
        assert all(len(ev.pcm) == buzzkit.HUDDLE_FRAME_SAMPLES * 2 for ev in audio)
        assert [ev.seq for ev in audio] == [0, 1]
        assert not any(ev.is_dtx for ev in audio)

        assert HuddlePeerJoined(pubkey=new_peer, peer_index=2) in events
        assert HuddlePeerLeft(pubkey=new_peer, peer_index=2) in events

    async def test_own_join_echo_is_not_an_event(self):
        async def handler(ws):
            auth = await relay_handshake(ws)
            await ws.send(
                json.dumps({"type": "joined", "pubkey": auth["event"]["pubkey"], "peer_index": 1})
            )

        async with serve(handler) as url:
            client = make_client(url)
            await client.connect()
            events = [ev async for ev in client.events()]
            await client.close()
        assert events == []


class TestOutbound:
    async def test_send_pcm_is_paced_never_bursts(self):
        """A batch of queued audio is emitted one frame per ~20 ms, in order,
        never dumped as a burst (which a receiver's jitter buffer mishandles)."""
        arrivals: list[float] = []
        received: list[bytes] = []
        done = asyncio.Event()

        async def handler(ws):
            await relay_handshake(ws)
            async for msg in ws:
                if isinstance(msg, bytes):
                    arrivals.append(time.monotonic())
                    received.append(msg)
                    if len(received) == 10:
                        done.set()

        async with serve(handler) as url:
            client = make_client(url)
            await client.connect()
            client.send_pcm(sine_pcm(10))  # 10 frames queued at once
            assert client.queued_frames == 10
            async with asyncio.timeout(5):
                await done.wait()
            await client.close()

        # 10 frames at 20 ms spacing ≈ 0.18 s of wall clock; a burst would
        # arrive in a few ms. Allow slack but require real pacing.
        span = arrivals[-1] - arrivals[0]
        assert span >= 0.12, f"10 frames spanned {span:.3f}s — sent as a burst"
        seqs = [int.from_bytes(f[0:2], "big") for f in received]
        assert seqs == list(range(10))

    async def test_unpaced_relays_immediately_in_order(self):
        """paced=False: frames go to the wire as fast as queued, in order, with
        no pacing sleep and no injected silence (an external pacer owns timing)."""
        received: list[bytes] = []
        done = asyncio.Event()

        async def handler(ws):
            await relay_handshake(ws)
            async for msg in ws:
                if isinstance(msg, bytes):
                    received.append(msg)
                    if len(received) == 10:
                        done.set()

        async with serve(handler) as url:
            client = make_client(url, paced=False, stream_silence=True)
            await client.connect()
            start = time.monotonic()
            client.send_pcm(sine_pcm(10))
            async with asyncio.timeout(5):
                await done.wait()
            elapsed = time.monotonic() - start
            await client.close()

        # Unpaced: 10 frames relayed near-instantly, not spread over ~0.2 s.
        assert elapsed < 0.1, f"unpaced relay took {elapsed:.3f}s — still pacing?"
        seqs = [int.from_bytes(f[0:2], "big") for f in received]
        assert seqs == list(range(10))

    async def test_clear_queue_drops_pending_audio(self):
        async def handler(ws):
            await relay_handshake(ws)
            await ws.wait_closed()

        async with serve(handler) as url:
            client = make_client(url)
            await client.connect()
            # Half a frame stays in the encoder; whole frames are queued.
            client.send_pcm(sine_pcm(3) + b"\x00\x00" * 100)
            assert client.queued_frames >= 1
            dropped = client.clear_queue()
            assert dropped >= 1
            assert client.queued_frames == 0
            # The buffered partial was discarded too: flush emits nothing.
            client.flush_pcm()
            assert client.queued_frames == 0
            await client.close()

    async def test_flush_pcm_emits_padded_tail(self):
        async def handler(ws):
            await relay_handshake(ws)
            await ws.wait_closed()

        async with serve(handler) as url:
            client = make_client(url)
            await client.connect()
            client.send_pcm(b"\x01\x00" * 100)  # < one frame
            assert client.queued_frames == 0
            client.flush_pcm()
            assert client.queued_frames == 1
            await client.close()

    async def test_idle_stream_is_continuous_and_monotonic(self):
        """Like a live mic: idle still sends frames, and speech queued after an
        idle stretch continues the same seq/ts timeline on the wire."""
        received: list[bytes] = []
        enough = asyncio.Event()

        async def handler(ws):
            await relay_handshake(ws)
            async for msg in ws:
                if isinstance(msg, bytes):
                    received.append(msg)
                    if len(received) >= 12:
                        enough.set()

        async with serve(handler) as url:
            client = make_client(url, stream_silence=True)
            await client.connect()
            await asyncio.sleep(0.12)  # idle: silence frames must flow
            client.send_pcm(sine_pcm(3))  # then speech
            async with asyncio.timeout(5):
                await enough.wait()
            await client.close()

        assert len(received) >= 12
        seqs = [int.from_bytes(f[0:2], "big") for f in received]
        assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs), (
            f"wire seq must be strictly monotonic, got {seqs}"
        )
        levels = {f[6] for f in received}
        assert len(levels) > 1, "expected both silence-floor and speech levels"

    async def test_leave_sends_leave_message(self):
        got_leave = asyncio.Event()

        async def handler(ws):
            await relay_handshake(ws)
            async for msg in ws:
                if isinstance(msg, str) and json.loads(msg).get("type") == "leave":
                    got_leave.set()

        async with serve(handler) as url:
            client = make_client(url)
            await client.connect()
            await client.leave()
            async with asyncio.timeout(2):
                await got_leave.wait()
