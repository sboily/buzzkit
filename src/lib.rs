//! PyO3 bindings over Block's Buzz zero-I/O crates.
//!
//! Design: bind only the pure, CPU-bound parts that are dangerous to
//! reimplement (Schnorr signing, event building, verification, NIP-42/98 auth
//! events). All network I/O (WebSocket/HTTP to the relay) lives in Python.
//! This keeps the FFI synchronous and tiny — no tokio <-> asyncio bridge.

mod huddle;

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use nostr::{EventBuilder, JsonUtil, Keys, Kind, PublicKey, RelayUrl, Tag, ToBech32};
use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use sha2::{Digest, Sha256};
use uuid::Uuid;

fn keys_from_secret(secret: &str) -> PyResult<Keys> {
    Keys::parse(secret).map_err(|e| PyValueError::new_err(format!("invalid secret key: {e}")))
}

fn bech32<T: ToBech32>(v: &T) -> PyResult<String>
where
    <T as ToBech32>::Err: std::fmt::Display,
{
    v.to_bech32()
        .map_err(|e| PyValueError::new_err(format!("bech32 encode failed: {e}")))
}

/// Generate a fresh Nostr keypair. Returns `(nsec, npub, pubkey_hex)`.
#[pyfunction]
fn generate_keypair() -> PyResult<(String, String, String)> {
    let keys = Keys::generate();
    Ok((
        bech32(keys.secret_key())?,
        bech32(&keys.public_key())?,
        keys.public_key().to_hex(),
    ))
}

/// Derive `(npub, pubkey_hex)` from a secret key (hex or `nsec…`).
#[pyfunction]
fn pubkey_from_secret(secret: &str) -> PyResult<(String, String)> {
    let keys = keys_from_secret(secret)?;
    Ok((bech32(&keys.public_key())?, keys.public_key().to_hex()))
}

/// Build and sign a channel chat message (kind 9). Returns the NIP-01 event JSON.
///
/// `channel_id` must be a UUID; `mentions` are pubkey hex strings (optional).
#[pyfunction]
#[pyo3(signature = (secret, channel_id, content, mentions=None))]
fn build_message_event(
    secret: &str,
    channel_id: &str,
    content: &str,
    mentions: Option<Vec<String>>,
) -> PyResult<String> {
    let keys = keys_from_secret(secret)?;
    let cid = Uuid::parse_str(channel_id)
        .map_err(|e| PyValueError::new_err(format!("channel_id must be a UUID: {e}")))?;
    let mentions = mentions.unwrap_or_default();
    let mention_refs: Vec<&str> = mentions.iter().map(String::as_str).collect();
    let builder = buzz_sdk::build_message(cid, content, None, &mention_refs, false, &[])
        .map_err(|e| PyValueError::new_err(format!("build_message: {e}")))?;
    let event = builder
        .sign_with_keys(&keys)
        .map_err(|e| PyValueError::new_err(format!("sign: {e}")))?;
    Ok(event.as_json())
}

/// Build and sign a NIP-42 AUTH event (kind 22242) for a relay challenge.
/// Returns the event JSON to send back as `["AUTH", <event>]`.
///
/// `auth_tag` is an optional NIP-OA owner-attestation tag JSON
/// (`["auth", <owner>, <conditions>, <sig>]`) — inject it so the relay records
/// the agent's owner.
#[pyfunction]
#[pyo3(signature = (secret, challenge, relay_url, auth_tag=None))]
fn build_auth_event(
    secret: &str,
    challenge: &str,
    relay_url: &str,
    auth_tag: Option<&str>,
) -> PyResult<String> {
    let keys = keys_from_secret(secret)?;
    let url = RelayUrl::parse(relay_url)
        .map_err(|e| PyValueError::new_err(format!("invalid relay url: {e}")))?;
    let mut builder = EventBuilder::auth(challenge, url);
    if let Some(tag_json) = auth_tag {
        let tag = buzz_sdk::nip_oa::parse_auth_tag(tag_json)
            .map_err(|e| PyValueError::new_err(format!("invalid auth_tag: {e}")))?;
        builder = builder.tags([tag]);
    }
    let event = builder
        .sign_with_keys(&keys)
        .map_err(|e| PyValueError::new_err(format!("sign: {e}")))?;
    Ok(event.as_json())
}

/// Compute a NIP-OA owner-attestation tag: the OWNER signs an attestation over
/// the agent's pubkey. Returns the `["auth", <owner>, <conditions>, <sig>]` tag
/// JSON. `owner_secret` must differ from the agent (no self-attestation).
#[pyfunction]
#[pyo3(signature = (owner_secret, agent_pubkey_hex, conditions=""))]
fn compute_auth_tag(
    owner_secret: &str,
    agent_pubkey_hex: &str,
    conditions: &str,
) -> PyResult<String> {
    let owner_keys = keys_from_secret(owner_secret)?;
    let agent_pubkey = PublicKey::from_hex(agent_pubkey_hex)
        .map_err(|e| PyValueError::new_err(format!("invalid agent pubkey: {e}")))?;
    buzz_sdk::nip_oa::compute_auth_tag(&owner_keys, &agent_pubkey, conditions)
        .map_err(|e| PyValueError::new_err(format!("compute_auth_tag: {e}")))
}

/// Sign a NIP-98 HTTP-auth event (kind 27235). Returns the
/// `Authorization: Nostr <base64>` header value for the Buzz HTTP bridge.
#[pyfunction]
#[pyo3(signature = (secret, method, url, body=None))]
fn sign_nip98(secret: &str, method: &str, url: &str, body: Option<Vec<u8>>) -> PyResult<String> {
    let keys = keys_from_secret(secret)?;
    let nonce = Uuid::new_v4().to_string();
    let mut tags = vec![
        Tag::parse(["u", url]).map_err(|e| PyValueError::new_err(e.to_string()))?,
        Tag::parse(["method", method]).map_err(|e| PyValueError::new_err(e.to_string()))?,
        Tag::parse(["nonce", nonce.as_str()]).map_err(|e| PyValueError::new_err(e.to_string()))?,
    ];
    if let Some(b) = body {
        let hash = hex::encode(Sha256::digest(&b));
        tags.push(
            Tag::parse(["payload", hash.as_str()])
                .map_err(|e| PyValueError::new_err(e.to_string()))?,
        );
    }
    let event = EventBuilder::new(Kind::Custom(27235), "")
        .tags(tags)
        .sign_with_keys(&keys)
        .map_err(|e| PyValueError::new_err(format!("NIP-98 sign: {e}")))?;
    Ok(format!("Nostr {}", B64.encode(event.as_json().as_bytes())))
}

/// Verify an event's id and Schnorr signature. Returns `True` if valid.
#[pyfunction]
fn verify_event(event_json: &str) -> PyResult<bool> {
    let event = nostr::Event::from_json(event_json)
        .map_err(|e| PyValueError::new_err(format!("parse event: {e}")))?;
    Ok(buzz_core::verify_event(&event).is_ok())
}

/// Build and sign a profile event (kind 0). Returns the NIP-01 event JSON.
///
/// `auth_tag` optionally embeds a NIP-OA owner-attestation tag in the profile;
/// the Buzz desktop reads the agent's kind-0 profile for this tag to show the
/// agent as "managed by <owner>".
#[pyfunction]
#[pyo3(signature = (secret, display_name=None, name=None, about=None, picture=None, nip05=None, auth_tag=None))]
fn build_profile_event(
    secret: &str,
    display_name: Option<String>,
    name: Option<String>,
    about: Option<String>,
    picture: Option<String>,
    nip05: Option<String>,
    auth_tag: Option<&str>,
) -> PyResult<String> {
    let keys = keys_from_secret(secret)?;
    let mut builder = buzz_sdk::build_profile(
        display_name.as_deref(),
        name.as_deref(),
        picture.as_deref(),
        about.as_deref(),
        nip05.as_deref(),
    )
    .map_err(|e| PyValueError::new_err(format!("build_profile: {e}")))?;
    if let Some(tag_json) = auth_tag {
        let tag = buzz_sdk::nip_oa::parse_auth_tag(tag_json)
            .map_err(|e| PyValueError::new_err(format!("invalid auth_tag: {e}")))?;
        builder = builder.tags([tag]);
    }
    let event = builder
        .sign_with_keys(&keys)
        .map_err(|e| PyValueError::new_err(format!("sign: {e}")))?;
    Ok(event.as_json())
}

/// Build and sign a NIP-29 channel self-join event (kind 9000, role=bot).
/// The signer adds itself (`p` = own pubkey) to `channel_id`'s member list.
#[pyfunction]
fn build_join_channel_event(secret: &str, channel_id: &str) -> PyResult<String> {
    let keys = keys_from_secret(secret)?;
    let cid = Uuid::parse_str(channel_id)
        .map_err(|e| PyValueError::new_err(format!("channel_id must be a UUID: {e}")))?;
    let pubkey_hex = keys.public_key().to_hex();
    // The `p` tag references the signer itself (self-join), so we must opt into
    // self-tagging — nostr's EventBuilder strips author-matching `p` tags by
    // default, which the relay would then reject as "missing p tag".
    let builder = buzz_sdk::build_add_member(cid, &pubkey_hex, Some(buzz_sdk::MemberRole::Bot))
        .map_err(|e| PyValueError::new_err(format!("build_add_member: {e}")))?
        .allow_self_tagging();
    let event = builder
        .sign_with_keys(&keys)
        .map_err(|e| PyValueError::new_err(format!("sign: {e}")))?;
    Ok(event.as_json())
}

/// Build and sign a NIP-29 create-channel event (kind 9007).
///
/// `visibility` is "open" or "private" (relay default applies when omitted);
/// `channel_type` is "stream", "forum", "dm", or "workflow". A `ttl` in
/// seconds makes the channel ephemeral (huddles use private/stream/3600).
/// Management kinds go over the WebSocket, not the HTTP bridge.
#[pyfunction]
#[pyo3(signature = (secret, channel_id, name, visibility=None, channel_type=None, about=None, ttl=None))]
fn build_create_channel_event(
    secret: &str,
    channel_id: &str,
    name: &str,
    visibility: Option<&str>,
    channel_type: Option<&str>,
    about: Option<&str>,
    ttl: Option<i32>,
) -> PyResult<String> {
    let keys = keys_from_secret(secret)?;
    let cid = Uuid::parse_str(channel_id)
        .map_err(|e| PyValueError::new_err(format!("channel_id must be a UUID: {e}")))?;
    let visibility = match visibility {
        None => None,
        Some("open") => Some(buzz_sdk::Visibility::Open),
        Some("private") => Some(buzz_sdk::Visibility::Private),
        Some(v) => {
            return Err(PyValueError::new_err(format!(
                "visibility must be \"open\" or \"private\", got {v:?}"
            )))
        }
    };
    let channel_type = match channel_type {
        None => None,
        Some("stream") => Some(buzz_sdk::ChannelKind::Stream),
        Some("forum") => Some(buzz_sdk::ChannelKind::Forum),
        Some("dm") => Some(buzz_sdk::ChannelKind::Dm),
        Some("workflow") => Some(buzz_sdk::ChannelKind::Workflow),
        Some(t) => {
            return Err(PyValueError::new_err(format!(
                "channel_type must be stream/forum/dm/workflow, got {t:?}"
            )))
        }
    };
    let event = buzz_sdk::build_create_channel(cid, name, visibility, channel_type, about, ttl)
        .map_err(|e| PyValueError::new_err(format!("build_create_channel: {e}")))?
        .sign_with_keys(&keys)
        .map_err(|e| PyValueError::new_err(format!("sign: {e}")))?;
    Ok(event.as_json())
}

/// Build and sign a huddle-started advisory (kind 48100), posted to the
/// PARENT channel. Content carries the ephemeral huddle channel id; the
/// relay validates this creator-signed link when others join the huddle's
/// audio room via `parent_channel_id`.
#[pyfunction]
fn build_huddle_started_event(
    secret: &str,
    parent_channel_id: &str,
    ephemeral_channel_id: &str,
) -> PyResult<String> {
    let keys = keys_from_secret(secret)?;
    let parent = Uuid::parse_str(parent_channel_id)
        .map_err(|e| PyValueError::new_err(format!("parent_channel_id must be a UUID: {e}")))?;
    let ephemeral = Uuid::parse_str(ephemeral_channel_id)
        .map_err(|e| PyValueError::new_err(format!("ephemeral_channel_id must be a UUID: {e}")))?;
    let content = format!("{{\"ephemeral_channel_id\":\"{ephemeral}\"}}");
    let h_tag =
        Tag::parse(["h", &parent.to_string()]).map_err(|e| PyValueError::new_err(e.to_string()))?;
    let event = EventBuilder::new(Kind::Custom(48100), content)
        .tags([h_tag])
        .sign_with_keys(&keys)
        .map_err(|e| PyValueError::new_err(format!("sign: {e}")))?;
    Ok(event.as_json())
}

/// Build and sign a presence event (kind 20001). `status` is "online",
/// "away", or "offline". Ephemeral — publish over the WebSocket.
#[pyfunction]
#[pyo3(signature = (secret, status="online"))]
fn build_presence_event(secret: &str, status: &str) -> PyResult<String> {
    let keys = keys_from_secret(secret)?;
    let event = buzz_sdk::build_presence_update(status)
        .map_err(|e| PyValueError::new_err(format!("build_presence: {e}")))?
        .sign_with_keys(&keys)
        .map_err(|e| PyValueError::new_err(format!("sign: {e}")))?;
    Ok(event.as_json())
}

#[pymodule]
fn _native(m: &Bound<'_, PyModule>) -> PyResult<()> {
    m.add_function(wrap_pyfunction!(generate_keypair, m)?)?;
    m.add_function(wrap_pyfunction!(pubkey_from_secret, m)?)?;
    m.add_function(wrap_pyfunction!(build_message_event, m)?)?;
    m.add_function(wrap_pyfunction!(build_auth_event, m)?)?;
    m.add_function(wrap_pyfunction!(compute_auth_tag, m)?)?;
    m.add_function(wrap_pyfunction!(sign_nip98, m)?)?;
    m.add_function(wrap_pyfunction!(verify_event, m)?)?;
    m.add_function(wrap_pyfunction!(build_profile_event, m)?)?;
    m.add_function(wrap_pyfunction!(build_join_channel_event, m)?)?;
    m.add_function(wrap_pyfunction!(build_create_channel_event, m)?)?;
    m.add_function(wrap_pyfunction!(build_huddle_started_event, m)?)?;
    m.add_function(wrap_pyfunction!(build_presence_event, m)?)?;

    // Buzz event kinds (subset — mirrors buzz-core/src/kind.rs).
    m.add("KIND_REACTION", 7u16)?;
    m.add("KIND_STREAM_MESSAGE", 9u16)?;
    m.add("KIND_PRESENCE_UPDATE", 20001u16)?;
    m.add("KIND_AUTH", 22242u16)?;
    m.add("KIND_HTTP_AUTH", 27235u16)?;
    m.add("KIND_STREAM_MESSAGE_V2", 40002u16)?;
    m.add("KIND_ADD_MEMBER", 9000u16)?;
    m.add("KIND_CREATE_CHANNEL", 9007u16)?;
    m.add("KIND_HUDDLE_STARTED", 48100u16)?;
    m.add("KIND_HUDDLE_PARTICIPANT_JOINED", 48101u16)?;
    m.add("KIND_HUDDLE_PARTICIPANT_LEFT", 48102u16)?;
    m.add("KIND_HUDDLE_ENDED", 48103u16)?;

    // Huddle audio (Opus over the /huddle/{channel_id}/audio WebSocket).
    m.add_class::<huddle::HuddleEncoder>()?;
    m.add_class::<huddle::HuddleDecoder>()?;
    m.add("HUDDLE_PROTOCOL_VERSION", huddle::PROTOCOL_VERSION)?;
    m.add("HUDDLE_SAMPLE_RATE", huddle::SAMPLE_RATE)?;
    m.add("HUDDLE_FRAME_SAMPLES", huddle::FRAME_SAMPLES)?;
    Ok(())
}
