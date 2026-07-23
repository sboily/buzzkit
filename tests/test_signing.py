"""Offline unit tests — no relay needed. Exercise the Rust binding surface."""

from __future__ import annotations

import json
import uuid

import buzzkit
import pytest


def test_keypair_roundtrip():
    nsec, npub, pk_hex = buzzkit.generate_keypair()
    assert nsec.startswith("nsec1")
    assert npub.startswith("npub1")
    assert len(pk_hex) == 64
    assert buzzkit.pubkey_from_secret(nsec) == (npub, pk_hex)


def test_message_sign_and_verify():
    nsec, _, pk_hex = buzzkit.generate_keypair()
    channel_id = str(uuid.uuid4())
    event_json = buzzkit.build_message_event(nsec, channel_id, "hello", None)
    event = json.loads(event_json)

    assert event["kind"] == buzzkit.KIND_STREAM_MESSAGE == 9
    assert event["pubkey"] == pk_hex
    assert ["h", channel_id] in event["tags"]
    assert buzzkit.verify_event(event_json) is True


def test_tamper_is_detected():
    nsec, _, _ = buzzkit.generate_keypair()
    event = json.loads(buzzkit.build_message_event(nsec, str(uuid.uuid4()), "x", None))
    event["content"] = "tampered"
    assert buzzkit.verify_event(json.dumps(event)) is False


def test_invalid_channel_id_raises():
    nsec, _, _ = buzzkit.generate_keypair()
    with pytest.raises(ValueError):
        buzzkit.build_message_event(nsec, "not-a-uuid", "x", None)


def test_profile_event():
    nsec, _, _ = buzzkit.generate_keypair()
    event = json.loads(buzzkit.build_profile_event(nsec, display_name="Bot", about="test"))
    assert event["kind"] == 0
    content = json.loads(event["content"])
    assert content["display_name"] == "Bot"
    assert content["about"] == "test"


def test_nip98_header_shape():
    nsec, _, _ = buzzkit.generate_keypair()
    header = buzzkit.sign_nip98(nsec, "POST", "https://relay.example/events", b"{}")
    assert header.startswith("Nostr ")


def test_auth_event_is_kind_22242():
    nsec, _, _ = buzzkit.generate_keypair()
    event = json.loads(
        buzzkit.build_auth_event(nsec, "challenge-123", "wss://relay.example")
    )
    assert event["kind"] == buzzkit.KIND_AUTH == 22242
    assert ["challenge", "challenge-123"] in event["tags"]
