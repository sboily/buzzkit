"""NIP-OA agent ownership verification.

Mirrors the reference algorithm from upstream buzz-cli (#3178): an agent's
kind-0 profile asserts ownership only when it carries *exactly one* `auth`
tag whose Schnorr signature verifies against the claimed owner AND whose
conditions (`kind=…`, `created_at<…`, `created_at>…`) apply to the profile
event itself.

Pure functions — no I/O. :meth:`BuzzClient.resolve_agent` does the relay side.
"""

from __future__ import annotations

import json
import string
from typing import Any

from . import _native

_HEX_LOWER = set(string.digits + "abcdef")

#: Possible return values of :func:`verify_agent_profile`, mirroring upstream.
VERIFICATIONS = (
    "verified",
    "missing_auth",
    "multiple_auth_tags",
    "invalid_auth",
    "owner_mismatch",
    "condition_mismatch",
    "invalid_agent_pubkey",
)


def _auth_tags(event: dict[str, Any]) -> list[list]:
    tags = event.get("tags")
    if not isinstance(tags, list):
        return []
    return [t for t in tags if isinstance(t, list) and t and t[0] == "auth"]


def _conditions_apply(conditions: str, event: dict[str, Any]) -> bool:
    """Do the auth tag's conditions hold for this event? Empty clauses hold."""
    kind = event.get("kind")
    created_at = event.get("created_at")
    if not isinstance(kind, int) or not isinstance(created_at, int):
        return False
    for clause in conditions.split("&"):
        if clause.startswith("kind="):
            ok = clause[len("kind=") :] == str(kind)
        elif clause.startswith("created_at<"):
            bound = clause[len("created_at<") :]
            ok = bound.isdigit() and created_at < int(bound)
        elif clause.startswith("created_at>"):
            bound = clause[len("created_at>") :]
            ok = bound.isdigit() and created_at > int(bound)
        else:
            ok = clause == ""
        if not ok:
            return False
    return True


def verify_agent_profile(profile_event: dict[str, Any], owner_pubkey_hex: str) -> str:
    """Verify that an agent's kind-0 profile is attested by ``owner_pubkey_hex``.

    Returns one of :data:`VERIFICATIONS`; only ``"verified"`` asserts
    ownership. ``profile_event`` is the parsed NIP-01 event dict.
    """
    agent_pubkey = profile_event.get("pubkey")
    if (
        not isinstance(agent_pubkey, str)
        or len(agent_pubkey) != 64
        or not set(agent_pubkey) <= _HEX_LOWER
    ):
        return "invalid_agent_pubkey"
    tags = _auth_tags(profile_event)
    if not tags:
        return "missing_auth"
    if len(tags) > 1:
        return "multiple_auth_tags"
    tag = tags[0]
    try:
        owner = _native.verify_auth_tag(json.dumps(tag), agent_pubkey)
    except ValueError as e:
        # from_hex accepted 64 lowercase hex that is not a curve point
        return "invalid_agent_pubkey" if "invalid agent pubkey" in str(e) else "invalid_auth"
    if owner != owner_pubkey_hex:
        return "owner_mismatch"
    if not _conditions_apply(tag[2], profile_event):
        return "condition_mismatch"
    return "verified"


#: Owner control commands the Buzz platform defines. The message content,
#: trimmed, must equal ``!<command>`` exactly — ``!shutdown please`` is a
#: regular message, not a command (mirrors upstream ``buzz-acp``).
OWNER_COMMANDS = ("shutdown", "cancel", "rotate")


def parse_owner_command(event: dict[str, Any], agent_pubkey_hex: str) -> str | None:
    """Match a Buzz owner control command addressed to ``agent_pubkey_hex``.

    Mirrors upstream ``buzz-acp``'s ``is_owner_control_command``: a control
    command is a chat message (kind 9) whose trimmed content is exactly one
    of :data:`OWNER_COMMANDS` prefixed with ``!``, and which mentions the
    agent through a ``["p", <agent_pubkey>]`` tag. Returns the bare command
    name (``"shutdown"``, ``"cancel"``, ``"rotate"``) or ``None``.

    Wire matching only — verifying that the event's *author* is the agent's
    owner is the caller's job (see :attr:`BuzzClient.verified_owner_hex`):
    upstream treats a non-owner ``!shutdown`` as a regular message, and so
    must callers.
    """
    if event.get("kind") != _native.KIND_STREAM_MESSAGE:
        return None
    content = str(event.get("content", "") or "").strip()
    if not content.startswith("!") or content[1:] not in OWNER_COMMANDS:
        return None
    tags = event.get("tags")
    if not isinstance(tags, list):
        return None
    mentioned = any(
        isinstance(t, list) and len(t) >= 2 and t[0] == "p" and t[1] == agent_pubkey_hex
        for t in tags
    )
    return content[1:] if mentioned else None
