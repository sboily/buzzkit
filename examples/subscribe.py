"""Real-time inbound: subscribe to a channel over the WebSocket and print messages.

BUZZ_RELAY_URL=wss://... python examples/subscribe.py <channel-uuid>
"""

from __future__ import annotations

import asyncio
import sys

from _shared import agent_secret, relay_url
from buzzkit import BuzzClient


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: subscribe.py <channel-uuid>")
    channel_id = sys.argv[1]

    async with BuzzClient(relay_url(), agent_secret()) as bz:
        print(f"listening on {channel_id} as {bz.npub} (Ctrl-C to stop) ...")
        async for event in bz.subscribe_channel(channel_id):
            print(f"{event['pubkey'][:8]}: {event['content']}")


if __name__ == "__main__":
    asyncio.run(main())
