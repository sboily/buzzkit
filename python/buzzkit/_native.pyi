"""Type stubs for the Rust extension module (buzzkit._native)."""

def generate_keypair() -> tuple[str, str, str]:
    """Return ``(nsec, npub, pubkey_hex)`` for a fresh keypair."""

def pubkey_from_secret(secret: str) -> tuple[str, str]:
    """Return ``(npub, pubkey_hex)`` for a secret (hex or ``nsec…``)."""

def build_message_event(
    secret: str,
    channel_id: str,
    content: str,
    mentions: list[str] | None = ...,
    reply_to: str | None = ...,
    reply_root: str | None = ...,
) -> str:
    """Build + sign a channel message (kind 9); returns NIP-01 event JSON.

    ``reply_to`` threads the message; ``reply_root`` marks a nested reply.
    """

def build_reaction_event(secret: str, target_event_id: str, emoji: str) -> str:
    """Build + sign a reaction (kind 7) to an event."""

def build_remove_reaction_event(secret: str, reaction_event_id: str) -> str:
    """Build + sign a deletion (kind 5) of one of our own reactions."""

def build_edit_event(secret: str, channel_id: str, target_event_id: str, new_content: str) -> str:
    """Build + sign a message edit (kind 40003)."""

def build_delete_message_event(
    secret: str, channel_id: str, target_event_id: str, reason: str | None = ...
) -> str:
    """Build + sign a message delete tombstone (kind 9005)."""

def build_set_topic_event(secret: str, channel_id: str, topic: str) -> str:
    """Build + sign a channel topic change (kind 9002)."""

def build_leave_event(secret: str, channel_id: str) -> str:
    """Build + sign a channel leave request (kind 9022)."""

def build_profile_event(
    secret: str,
    display_name: str | None = ...,
    name: str | None = ...,
    about: str | None = ...,
    picture: str | None = ...,
    nip05: str | None = ...,
    auth_tag: str | None = ...,
) -> str:
    """Build + sign a profile event (kind 0); returns NIP-01 event JSON.

    ``auth_tag`` embeds a NIP-OA owner attestation so the Buzz desktop shows
    the identity as "managed by <owner>".
    """

def build_join_channel_event(secret: str, channel_id: str) -> str:
    """Build + sign a NIP-29 channel self-join event (kind 9000, role=bot)."""

def build_create_channel_event(
    secret: str,
    channel_id: str,
    name: str,
    visibility: str | None = ...,
    channel_type: str | None = ...,
    about: str | None = ...,
    ttl: int | None = ...,
) -> str:
    """Build + sign a NIP-29 create-channel event (kind 9007)."""

def build_huddle_started_event(
    secret: str, parent_channel_id: str, ephemeral_channel_id: str
) -> str:
    """Build + sign a huddle-started advisory (kind 48100) for the parent channel."""

def build_presence_event(secret: str, status: str = ...) -> str:
    """Build + sign a presence event (kind 20001); status online/away/offline."""

def build_user_status_event(secret: str, text: str, emoji: str | None = ...) -> str:
    """Build + sign a NIP-38 user status event (kind 30315, ``d:general``)."""

def build_auth_event(
    secret: str, challenge: str, relay_url: str, auth_tag: str | None = ...
) -> str:
    """Build + sign a NIP-42 AUTH event (kind 22242); returns event JSON."""

def compute_auth_tag(owner_secret: str, agent_pubkey_hex: str, conditions: str = ...) -> str:
    """Compute a NIP-OA owner-attestation tag JSON attesting an agent pubkey."""

def verify_auth_tag(auth_tag_json: str, agent_pubkey_hex: str) -> str:
    """Verify a NIP-OA auth tag against an agent pubkey; returns the owner hex."""

def sign_nip98(secret: str, method: str, url: str, body: bytes | None = ...) -> str:
    """Return an ``Authorization: Nostr <base64>`` header value (NIP-98)."""

def verify_event(event_json: str) -> bool:
    """Verify an event's id + Schnorr signature."""

class HuddleEncoder:
    """Stateful huddle audio encoder: s16le mono 48 kHz PCM in, v2 wire frames out."""

    def __init__(self, bitrate: int = 32000, dtx: bool = True) -> None: ...
    def encode(self, pcm: bytes) -> list[bytes]:
        """Feed PCM; returns complete 20 ms wire frames (partials are buffered)."""

    def flush(self) -> bytes | None:
        """Zero-pad and emit the buffered partial frame, if any."""

    def discard(self) -> None:
        """Drop buffered PCM without emitting it (barge-in)."""

class HuddleDecoder:
    """Stateful huddle audio decoder for relay frames ([peer_index][header][opus])."""

    def __init__(self) -> None: ...
    def decode(self, frame: bytes) -> tuple[int, int, int, int, bool, bytes]:
        """Return ``(peer_index, seq, ts_48k, level_dbov, is_dtx, pcm_s16le_48k)``."""

    def remove_peer(self, peer_index: int) -> None:
        """Forget a peer's decoder state (indexes are recycled by the relay)."""

KIND_DELETION: int
KIND_REACTION: int
KIND_STREAM_MESSAGE: int
KIND_EDIT_METADATA: int
KIND_DELETE_MESSAGE: int
KIND_LEAVE_CHANNEL: int
KIND_MESSAGE_EDIT: int
KIND_PRESENCE_UPDATE: int
KIND_AUTH: int
KIND_HTTP_AUTH: int
KIND_STREAM_MESSAGE_V2: int
KIND_ADD_MEMBER: int
KIND_CREATE_CHANNEL: int
KIND_MANAGED_AGENT: int
KIND_USER_STATUS: int
KIND_HUDDLE_STARTED: int
KIND_HUDDLE_PARTICIPANT_JOINED: int
KIND_HUDDLE_PARTICIPANT_LEFT: int
KIND_HUDDLE_ENDED: int
HUDDLE_PROTOCOL_VERSION: int
HUDDLE_SAMPLE_RATE: int
HUDDLE_FRAME_SAMPLES: int
