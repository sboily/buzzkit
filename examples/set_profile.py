"""Publish the agent's profile (kind 0) so it shows a display name in Buzz.

BUZZ_RELAY_URL=wss://... python examples/set_profile.py ["Display Name"]
"""

from __future__ import annotations

import asyncio
import sys

from _shared import agent_secret, relay_url
from buzzkit import BuzzClient


async def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else "buzzkit agent"

    bz = BuzzClient(relay_url(), agent_secret())
    print(await bz.set_profile(name, about="posted via buzzkit"))


if __name__ == "__main__":
    asyncio.run(main())
