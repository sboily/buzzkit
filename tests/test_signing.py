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


def test_message_reply_threading():
    nsec, _, _ = buzzkit.generate_keypair()
    channel_id = str(uuid.uuid4())
    parent = "a" * 64
    root = "b" * 64

    direct = json.loads(buzzkit.build_message_event(nsec, channel_id, "re", reply_to=parent))
    assert ["e", parent, "", "reply"] in direct["tags"]
    assert not any(t[-1] == "root" for t in direct["tags"])

    nested = json.loads(
        buzzkit.build_message_event(nsec, channel_id, "re", reply_to=parent, reply_root=root)
    )
    assert ["e", root, "", "root"] in nested["tags"]
    assert ["e", parent, "", "reply"] in nested["tags"]

    with pytest.raises(ValueError):
        buzzkit.build_message_event(nsec, channel_id, "re", reply_root=root)
    with pytest.raises(ValueError):
        buzzkit.build_message_event(nsec, channel_id, "re", reply_to="not-an-event-id")


def test_reaction_events():
    nsec, _, _ = buzzkit.generate_keypair()
    target = "c" * 64
    reaction = json.loads(buzzkit.build_reaction_event(nsec, target, "👍"))
    assert reaction["kind"] == buzzkit.KIND_REACTION == 7
    assert reaction["content"] == "👍"
    assert ["e", target] in reaction["tags"]

    removal = json.loads(buzzkit.build_remove_reaction_event(nsec, reaction["id"]))
    assert removal["kind"] == buzzkit.KIND_DELETION == 5
    assert ["e", reaction["id"]] in removal["tags"]


def test_edit_and_delete_message_events():
    nsec, _, _ = buzzkit.generate_keypair()
    channel_id = str(uuid.uuid4())
    target = "d" * 64

    edit = json.loads(buzzkit.build_edit_event(nsec, channel_id, target, "fixed"))
    assert edit["kind"] == buzzkit.KIND_MESSAGE_EDIT == 40003
    assert edit["content"] == "fixed"
    assert ["h", channel_id] in edit["tags"]
    assert ["e", target] in edit["tags"]

    delete = json.loads(buzzkit.build_delete_message_event(nsec, channel_id, target))
    assert delete["kind"] == buzzkit.KIND_DELETE_MESSAGE == 9005
    assert ["e", target] in delete["tags"]
    assert not any(t[0] == "public_reason" for t in delete["tags"])

    moderated = json.loads(
        buzzkit.build_delete_message_event(nsec, channel_id, target, reason="spam")
    )
    assert ["public_reason", "spam"] in moderated["tags"]


def test_topic_and_leave_events():
    nsec, _, _ = buzzkit.generate_keypair()
    channel_id = str(uuid.uuid4())

    topic = json.loads(buzzkit.build_set_topic_event(nsec, channel_id, "daily standup"))
    assert topic["kind"] == buzzkit.KIND_EDIT_METADATA == 9002
    assert ["topic", "daily standup"] in topic["tags"]

    leave = json.loads(buzzkit.build_leave_event(nsec, channel_id))
    assert leave["kind"] == buzzkit.KIND_LEAVE_CHANNEL == 9022
    assert ["h", channel_id] in leave["tags"]


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
    event = json.loads(buzzkit.build_auth_event(nsec, "challenge-123", "wss://relay.example"))
    assert event["kind"] == buzzkit.KIND_AUTH == 22242
    assert ["challenge", "challenge-123"] in event["tags"]


def test_join_channel_event_keeps_self_p_tag():
    nsec, _, pk_hex = buzzkit.generate_keypair()
    event = json.loads(buzzkit.build_join_channel_event(nsec, str(uuid.uuid4())))
    assert event["kind"] == buzzkit.KIND_ADD_MEMBER == 9000
    # The `p` tag references the signer itself; it must survive nostr's self-tag
    # stripping (allow_self_tagging) or the relay rejects the join as "missing p tag".
    assert ["p", pk_hex] in event["tags"]
    assert ["role", "bot"] in event["tags"]
    assert any(t[0] == "h" for t in event["tags"])


def test_compute_auth_tag():
    owner_nsec, _, owner_pk = buzzkit.generate_keypair()
    _, _, agent_pk = buzzkit.generate_keypair()
    tag = json.loads(buzzkit.compute_auth_tag(owner_nsec, agent_pk))
    assert tag[0] == "auth"
    assert tag[1] == owner_pk  # owner pubkey
    assert len(tag) == 4  # ["auth", owner, conditions, sig]


def test_compute_auth_tag_rejects_self():
    nsec, _, pk_hex = buzzkit.generate_keypair()
    with pytest.raises(ValueError):
        buzzkit.compute_auth_tag(nsec, pk_hex)  # owner == agent


def test_presence_event():
    nsec, _, _ = buzzkit.generate_keypair()
    event = json.loads(buzzkit.build_presence_event(nsec, "online"))
    assert event["kind"] == buzzkit.KIND_PRESENCE_UPDATE == 20001
    assert event["content"] == "online" or ["status", "online"] in event["tags"]


def test_create_channel_event():
    nsec, _, _ = buzzkit.generate_keypair()
    channel_id = str(uuid.uuid4())
    event = json.loads(
        buzzkit.build_create_channel_event(
            nsec, channel_id, "huddle-test", visibility="private", channel_type="stream", ttl=3600
        )
    )
    assert event["kind"] == buzzkit.KIND_CREATE_CHANNEL == 9007
    tags = {t[0]: t[1] for t in event["tags"]}
    assert tags["h"] == channel_id
    assert tags["name"] == "huddle-test"
    assert tags["visibility"] == "private"
    assert tags["channel_type"] == "stream"
    assert tags["ttl"] == "3600"
    with pytest.raises(ValueError):
        buzzkit.build_create_channel_event(nsec, channel_id, "x", visibility="bogus")


def test_create_channel_name_is_canonicalized():
    # Since buzz v0.5.x, build_create_channel strips leading '#' (and
    # whitespace) and rejects names that canonicalize to empty.
    nsec, _, _ = buzzkit.generate_keypair()
    event = json.loads(buzzkit.build_create_channel_event(nsec, str(uuid.uuid4()), "###dev"))
    tags = {t[0]: t[1] for t in event["tags"]}
    assert tags["name"] == "dev"
    with pytest.raises(ValueError):
        buzzkit.build_create_channel_event(nsec, str(uuid.uuid4()), "  ###  ")


def test_user_status_event():
    nsec, _, _ = buzzkit.generate_keypair()
    event = json.loads(buzzkit.build_user_status_event(nsec, "reviewing PRs", emoji="🤖"))
    assert event["kind"] == buzzkit.KIND_USER_STATUS == 30315
    assert event["content"] == "reviewing PRs"
    assert ["d", "general"] in event["tags"]
    assert ["emoji", "🤖"] in event["tags"]
    cleared = json.loads(buzzkit.build_user_status_event(nsec, ""))
    assert cleared["content"] == ""
    assert not any(t[0] == "emoji" for t in cleared["tags"])


def test_verify_auth_tag_roundtrip():
    owner_nsec, _, owner_pk = buzzkit.generate_keypair()
    _, _, agent_pk = buzzkit.generate_keypair()
    tag = buzzkit.compute_auth_tag(owner_nsec, agent_pk, "kind=0")
    assert buzzkit.verify_auth_tag(tag, agent_pk) == owner_pk
    # The signature binds the agent pubkey: any other agent must fail.
    _, _, other_pk = buzzkit.generate_keypair()
    with pytest.raises(ValueError):
        buzzkit.verify_auth_tag(tag, other_pk)
    with pytest.raises(ValueError):
        buzzkit.verify_auth_tag("not json", agent_pk)


def test_huddle_started_event():
    nsec, _, _ = buzzkit.generate_keypair()
    parent, ephemeral = str(uuid.uuid4()), str(uuid.uuid4())
    event = json.loads(buzzkit.build_huddle_started_event(nsec, parent, ephemeral))
    assert event["kind"] == buzzkit.KIND_HUDDLE_STARTED == 48100
    assert ["h", parent] in event["tags"]
    assert json.loads(event["content"])["ephemeral_channel_id"] == ephemeral
    assert buzzkit.verify_event(json.dumps(event))
