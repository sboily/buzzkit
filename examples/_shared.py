"""Shared helpers for the examples: read the relay URL + agent secret."""

from __future__ import annotations

import os
import pathlib


def relay_url() -> str:
    url = os.environ.get("BUZZ_RELAY_URL")
    if not url:
        raise SystemExit("set BUZZ_RELAY_URL (e.g. wss://your-community.communities.buzz.xyz)")
    return url


def agent_secret() -> str:
    secret = os.environ.get("BUZZ_NSEC")
    if not secret:
        path = pathlib.Path(__file__).resolve().parent.parent / "agent.secret"
        if path.exists():
            secret = path.read_text().strip()
    if not secret:
        raise SystemExit("set BUZZ_NSEC, or create an agent.secret file next to pyproject.toml")
    return secret
