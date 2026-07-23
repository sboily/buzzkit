//! PyO3 bindings over Block's Buzz zero-I/O crates.
//!
//! Design: bind only the pure, CPU-bound parts that are dangerous to
//! reimplement (Schnorr signing, event building, verification, NIP-42/98 auth
//! events). All network I/O (WebSocket/HTTP to the relay) lives in Python.
//! This keeps the FFI synchronous and tiny — no tokio <-> asyncio bridge.

use base64::engine::general_purpose::STANDARD as B64;
use base64::Engine as _;
use nostr::{EventBuilder, JsonUtil, Keys, Kind, RelayUrl, Tag, ToBech32};
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
#[pyfunction]
fn build_auth_event(secret: &str, challenge: &str, relay_url: &str) -> PyResult<String> {
    let keys = keys_from_secret(secret)?;
    let url = RelayUrl::parse(relay_url)
        .map_err(|e| PyValueError::new_err(format!("invalid relay url: {e}")))?;
    let event = EventBuilder::auth(challenge, url)
        .sign_with_keys(&keys)
        .map_err(|e| PyValueError::new_err(format!("sign: {e}")))?;
    Ok(event.as_json())
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
#[pyfunction]
#[pyo3(signature = (secret, display_name=None, name=None, about=None, picture=None, nip05=None))]
fn build_profile_event(
    secret: &str,
    display_name: Option<String>,
    name: Option<String>,
    about: Option<String>,
    picture: Option<String>,
    nip05: Option<String>,
) -> PyResult<String> {
    let keys = keys_from_secret(secret)?;
    let builder = buzz_sdk::build_profile(
        display_name.as_deref(),
        name.as_deref(),
        picture.as_deref(),
        about.as_deref(),
        nip05.as_deref(),
    )
    .map_err(|e| PyValueError::new_err(format!("build_profile: {e}")))?;
    let event = builder
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
    m.add_function(wrap_pyfunction!(sign_nip98, m)?)?;
    m.add_function(wrap_pyfunction!(verify_event, m)?)?;
    m.add_function(wrap_pyfunction!(build_profile_event, m)?)?;

    // Buzz event kinds (subset — mirrors buzz-core/src/kind.rs).
    m.add("KIND_REACTION", 7u16)?;
    m.add("KIND_STREAM_MESSAGE", 9u16)?;
    m.add("KIND_PRESENCE_UPDATE", 20001u16)?;
    m.add("KIND_AUTH", 22242u16)?;
    m.add("KIND_HTTP_AUTH", 27235u16)?;
    m.add("KIND_STREAM_MESSAGE_V2", 40002u16)?;
    Ok(())
}
