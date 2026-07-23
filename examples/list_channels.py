"""List channels (kind 39000 metadata) visible to the agent.

BUZZ_RELAY_URL=wss://... python examples/list_channels.py
"""

from __future__ import annotations

import asyncio

from _shared import agent_secret, relay_url
from buzzkit import BuzzClient


async def main() -> None:
    bz = BuzzClient(relay_url(), agent_secret())
    channels = await bz.list_channels()
    print(f"{len(channels)} channel(s):")
    for ch in channels:
        print(f"  {ch['channel_id']}  {ch['name']}")


if __name__ == "__main__":
    asyncio.run(main())
