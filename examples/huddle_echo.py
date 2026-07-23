"""Echo agent for Buzz huddles — repeats back everything it hears.

Joins a huddle and echoes remote audio with a short delay. Useful for
verifying the audio path end-to-end (desktop → relay → agent → relay →
desktop) without any AI provider.

    BUZZ_RELAY_URL=wss://... python examples/huddle_echo.py <parent_channel_id>

Watches the parent channel for huddle announcements (kind 48100) and joins
each one; pass a second argument to join a known huddle directly:

    python examples/huddle_echo.py <parent_channel_id> <huddle_channel_id>
"""

from __future__ import annotations

import asyncio
import json
import sys
import time

from _shared import agent_secret, relay_url
from buzzkit import KIND_HUDDLE_STARTED, BuzzClient, HuddleAudio, HuddleClient


async def echo_in_huddle(relay: str, secret: str, huddle_id: str, parent_id: str) -> None:
    async with HuddleClient(relay, secret, huddle_id, parent_channel_id=parent_id) as huddle:
        print(f"joined huddle {huddle_id} — echoing (peers: {len(huddle.peers) - 1})")
        async for ev in huddle.events():
            if isinstance(ev, HuddleAudio):
                huddle.send_pcm(ev.pcm)
            else:
                print(f"  {type(ev).__name__}: {ev.pubkey[:12]}…")
        print(f"huddle {huddle_id} ended")


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: huddle_echo.py <parent_channel_id> [huddle_channel_id]")
    parent_id = sys.argv[1]
    relay, secret = relay_url(), agent_secret()

    if len(sys.argv) > 2:
        await echo_in_huddle(relay, secret, sys.argv[2], parent_id)
        return

    print(f"watching {parent_id} for huddles…")
    async with BuzzClient(relay, secret) as bz:
        live = {"kinds": [KIND_HUDDLE_STARTED], "#h": [parent_id], "since": int(time.time())}
        async for event in bz.subscribe([live]):
            huddle_id = json.loads(event["content"]).get("ephemeral_channel_id")
            if huddle_id:
                await echo_in_huddle(relay, secret, huddle_id, parent_id)
                print(f"watching {parent_id} for the next huddle…")


if __name__ == "__main__":
    asyncio.run(main())
