"""buzzkit — Python bindings + async client for Block's Buzz (Nostr) protocol.

Low-level event building/signing/verification is done in Rust (``buzz-core`` /
``buzz-sdk``); all network I/O is pure Python. Use the module-level functions for
one-off event work, or :class:`BuzzClient` for a managed async relay connection.

This is an independent, unofficial project — not affiliated with Block, Inc.
"""

from __future__ import annotations

from ._native import (
    HUDDLE_FRAME_SAMPLES,
    HUDDLE_PROTOCOL_VERSION,
    HUDDLE_SAMPLE_RATE,
    KIND_ADD_MEMBER,
    KIND_AUTH,
    KIND_CREATE_CHANNEL,
    KIND_HTTP_AUTH,
    KIND_HUDDLE_ENDED,
    KIND_HUDDLE_PARTICIPANT_JOINED,
    KIND_HUDDLE_PARTICIPANT_LEFT,
    KIND_HUDDLE_STARTED,
    KIND_PRESENCE_UPDATE,
    KIND_REACTION,
    KIND_STREAM_MESSAGE,
    KIND_STREAM_MESSAGE_V2,
    HuddleDecoder,
    HuddleEncoder,
    build_auth_event,
    build_create_channel_event,
    build_huddle_started_event,
    build_join_channel_event,
    build_message_event,
    build_presence_event,
    build_profile_event,
    compute_auth_tag,
    generate_keypair,
    pubkey_from_secret,
    sign_nip98,
    verify_event,
)
from .client import BuzzClient
from .huddle import (
    HuddleAudio,
    HuddleClient,
    HuddleError,
    HuddleEvent,
    HuddlePeerJoined,
    HuddlePeerLeft,
)

__version__ = "0.1.3"

__all__ = [
    "HUDDLE_FRAME_SAMPLES",
    "HUDDLE_PROTOCOL_VERSION",
    "HUDDLE_SAMPLE_RATE",
    "KIND_ADD_MEMBER",
    "KIND_AUTH",
    "KIND_CREATE_CHANNEL",
    "KIND_HTTP_AUTH",
    "KIND_HUDDLE_ENDED",
    "KIND_HUDDLE_PARTICIPANT_JOINED",
    "KIND_HUDDLE_PARTICIPANT_LEFT",
    "KIND_HUDDLE_STARTED",
    "KIND_PRESENCE_UPDATE",
    "KIND_REACTION",
    "KIND_STREAM_MESSAGE",
    "KIND_STREAM_MESSAGE_V2",
    "BuzzClient",
    "HuddleAudio",
    "HuddleClient",
    "HuddleDecoder",
    "HuddleEncoder",
    "HuddleError",
    "HuddleEvent",
    "HuddlePeerJoined",
    "HuddlePeerLeft",
    "build_auth_event",
    "build_create_channel_event",
    "build_huddle_started_event",
    "build_join_channel_event",
    "build_message_event",
    "build_presence_event",
    "build_profile_event",
    "compute_auth_tag",
    "generate_keypair",
    "pubkey_from_secret",
    "sign_nip98",
    "verify_event",
]
