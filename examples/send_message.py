"""Post a message to a channel via the HTTP bridge.

    BUZZ_RELAY_URL=wss://... python examples/send_message.py <channel-uuid> ["message"]
"""

from __future__ import annotations

import asyncio
import sys

from _shared import agent_secret, relay_url
from buzzkit import BuzzClient


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: send_message.py <channel-uuid> [message]")
    channel_id = sys.argv[1]
    message = sys.argv[2] if len(sys.argv) > 2 else "hello from buzzkit \U0001f41d"

    bz = BuzzClient(relay_url(), agent_secret())
    print(await bz.send_message(channel_id, message))


if __name__ == "__main__":
    asyncio.run(main())
