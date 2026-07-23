"""buzzkit — Python bindings + async client for Block's Buzz (Nostr) protocol.

Low-level event building/signing/verification is done in Rust (``buzz-core`` /
``buzz-sdk``); all network I/O is pure Python. Use the module-level functions for
one-off event work, or :class:`BuzzClient` for a managed async relay connection.

This is an independent, unofficial project — not affiliated with Block, Inc.
"""

from __future__ import annotations

from ._native import (
    KIND_AUTH,
    KIND_HTTP_AUTH,
    KIND_PRESENCE_UPDATE,
    KIND_REACTION,
    KIND_STREAM_MESSAGE,
    KIND_STREAM_MESSAGE_V2,
    build_auth_event,
    build_message_event,
    build_profile_event,
    generate_keypair,
    pubkey_from_secret,
    sign_nip98,
    verify_event,
)
from .client import BuzzClient

__version__ = "0.1.0"

__all__ = [
    "KIND_AUTH",
    "KIND_HTTP_AUTH",
    "KIND_PRESENCE_UPDATE",
    "KIND_REACTION",
    "KIND_STREAM_MESSAGE",
    "KIND_STREAM_MESSAGE_V2",
    "BuzzClient",
    "build_auth_event",
    "build_message_event",
    "build_profile_event",
    "generate_keypair",
    "pubkey_from_secret",
    "sign_nip98",
    "verify_event",
]
