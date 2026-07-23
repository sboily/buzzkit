"""Type stubs for the Rust extension module (buzzkit._native)."""

def generate_keypair() -> tuple[str, str, str]:
    """Return ``(nsec, npub, pubkey_hex)`` for a fresh keypair."""

def pubkey_from_secret(secret: str) -> tuple[str, str]:
    """Return ``(npub, pubkey_hex)`` for a secret (hex or ``nsec…``)."""

def build_message_event(
    secret: str, channel_id: str, content: str, mentions: list[str] | None = ...
) -> str:
    """Build + sign a channel message (kind 9); returns NIP-01 event JSON."""

def build_profile_event(
    secret: str,
    display_name: str | None = ...,
    name: str | None = ...,
    about: str | None = ...,
    picture: str | None = ...,
    nip05: str | None = ...,
) -> str:
    """Build + sign a profile event (kind 0); returns NIP-01 event JSON."""

def build_join_channel_event(secret: str, channel_id: str) -> str:
    """Build + sign a NIP-29 channel self-join event (kind 9000, role=bot)."""

def build_auth_event(secret: str, challenge: str, relay_url: str) -> str:
    """Build + sign a NIP-42 AUTH event (kind 22242); returns event JSON."""

def sign_nip98(secret: str, method: str, url: str, body: bytes | None = ...) -> str:
    """Return an ``Authorization: Nostr <base64>`` header value (NIP-98)."""

def verify_event(event_json: str) -> bool:
    """Verify an event's id + Schnorr signature."""

KIND_REACTION: int
KIND_STREAM_MESSAGE: int
KIND_PRESENCE_UPDATE: int
KIND_AUTH: int
KIND_HTTP_AUTH: int
KIND_STREAM_MESSAGE_V2: int
KIND_ADD_MEMBER: int
