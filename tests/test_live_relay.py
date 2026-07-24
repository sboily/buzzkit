"""Live integration test against a real Buzz relay (opt-in).

Runs only when BUZZ_LIVE_RELAY is set to a relay WebSocket URL, e.g.::

    BUZZ_LIVE_RELAY=ws://localhost:3000 pytest tests/test_live_relay.py -v

The relay must be OPEN (no membership gate) — the test mints fresh
identities. It exercises the full huddle path against the relay's real
implementation: channel creation, huddle announcement, audio WS handshake
with membership auto-add, Opus framing both ways, and roster events.
"""

from __future__ import annotations

import asyncio
import math
import os

import buzzkit
import pytest
from buzzkit import BuzzClient, HuddleAudio, HuddleClient, HuddlePeerJoined

RELAY = os.environ.get("BUZZ_LIVE_RELAY")

pytestmark = pytest.mark.skipif(not RELAY, reason="BUZZ_LIVE_RELAY not set")


def sine_pcm(frames: int, freq: float = 440.0) -> bytes:
    n = frames * buzzkit.HUDDLE_FRAME_SAMPLES
    return b"".join(
        int(8000 * math.sin(2 * math.pi * freq * i / 48000)).to_bytes(2, "little", signed=True)
        for i in range(n)
    )


async def test_huddle_audio_round_trip():
    human_nsec, _, human_pk = buzzkit.generate_keypair()
    agent_nsec, _, agent_pk = buzzkit.generate_keypair()
    assert RELAY is not None

    # The "human" (huddle starter, like the desktop app) creates a parent
    # channel and starts a huddle in it.
    async with BuzzClient(RELAY, human_nsec) as human_bz:
        import uuid

        parent_id = str(uuid.uuid4())
        create = buzzkit.build_create_channel_event(
            human_nsec, parent_id, "huddle-live-test", visibility="open", channel_type="stream"
        )
        result = await human_bz.publish(create)
        assert result["accepted"], result["message"]

        # The agent self-joins the parent channel (kind 9000, role=bot) — the
        # production onboarding path; huddle membership then comes from the
        # parent via auto-add.
        async with BuzzClient(RELAY, agent_nsec) as agent_bz:
            joined = await agent_bz.join_channel(parent_id)
            assert joined["accepted"], joined["message"]

            huddle_id = await human_bz.start_huddle(parent_id, name="live-test")

            async with (
                HuddleClient(RELAY, human_nsec, huddle_id, parent_channel_id=parent_id) as human,
                HuddleClient(RELAY, agent_nsec, huddle_id, parent_channel_id=parent_id) as agent,
            ):
                # The human's roster must include the agent (or gain it via a
                # joined event) and vice versa.
                assert agent.peers, "agent joined with an empty roster"

                # Human speaks 1 s of sine; the agent echoes what it hears.
                echoed: list[HuddleAudio] = []

                async def agent_loop() -> None:
                    async for ev in agent.events():
                        if isinstance(ev, HuddleAudio):
                            echoed.append(ev)
                            agent.send_pcm(ev.pcm)
                        elif isinstance(ev, HuddlePeerJoined):
                            pass
                        if len(echoed) >= 40:
                            return

                heard_back: list[HuddleAudio] = []

                async def human_loop() -> None:
                    async for ev in human.events():
                        if isinstance(ev, HuddleAudio) and not ev.is_dtx:
                            heard_back.append(ev)
                            if len(heard_back) >= 20:
                                return

                agent_task = asyncio.create_task(agent_loop())
                human_task = asyncio.create_task(human_loop())
                human.send_pcm(sine_pcm(50))  # 1 s of audio, paced ~20 ms/frame

                async with asyncio.timeout(15):
                    await agent_task
                    await human_task

                # Agent heard the human's stream, decoded to 48 kHz PCM.
                assert len(echoed) >= 40
                assert all(ev.pubkey == human_pk for ev in echoed if ev.pubkey)
                assert all(len(ev.pcm) == buzzkit.HUDDLE_FRAME_SAMPLES * 2 for ev in echoed)
                # Human heard the agent's echo back through the relay.
                assert len(heard_back) >= 20
                assert all(ev.pubkey == agent_pk for ev in heard_back if ev.pubkey)
