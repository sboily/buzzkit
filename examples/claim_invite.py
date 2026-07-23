"""Redeem a relay invite so the agent becomes a community member.

BUZZ_RELAY_URL=wss://... python examples/claim_invite.py <invite_url_or_code>
"""

from __future__ import annotations

import asyncio
import sys

from _shared import agent_secret, relay_url
from buzzkit import BuzzClient


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit("usage: claim_invite.py <invite_url_or_code>")

    bz = BuzzClient(relay_url(), agent_secret())
    print(await bz.claim_invite(sys.argv[1]))


if __name__ == "__main__":
    asyncio.run(main())
