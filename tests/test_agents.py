"""Offline tests for NIP-OA agent ownership verification and resolution."""

from __future__ import annotations

import json

import buzzkit
from buzzkit.client import BuzzClient


def _profile_event(agent_pk: str, auth_tags: list[list], created_at: int = 100) -> dict:
    return {
        "pubkey": agent_pk,
        "kind": 0,
        "created_at": created_at,
        "content": '{"display_name":"Honey"}',
        "tags": auth_tags,
    }


def _keys() -> tuple[str, str]:
    nsec, _, pk = buzzkit.generate_keypair()
    return nsec, pk


def test_verified_requires_one_valid_auth_tag_from_owner():
    owner_nsec, owner_pk = _keys()
    foreign_nsec, _ = _keys()
    _, agent_pk = _keys()
    valid = json.loads(buzzkit.compute_auth_tag(owner_nsec, agent_pk, "kind=0"))
    foreign = json.loads(buzzkit.compute_auth_tag(foreign_nsec, agent_pk, "kind=0"))

    verify = buzzkit.verify_agent_profile
    assert verify(_profile_event(agent_pk, [valid]), owner_pk) == "verified"
    assert verify(_profile_event(agent_pk, [foreign]), owner_pk) == "owner_mismatch"
    assert verify(_profile_event(agent_pk, []), owner_pk) == "missing_auth"
    assert verify(_profile_event(agent_pk, [valid, valid]), owner_pk) == "multiple_auth_tags"
    forged = ["auth", owner_pk, "kind=0", "0" * 128]
    assert verify(_profile_event(agent_pk, [forged]), owner_pk) == "invalid_auth"
    assert verify(_profile_event("not-a-pubkey", [valid]), owner_pk) == "invalid_agent_pubkey"


def test_conditions_must_apply_to_the_profile_event():
    owner_nsec, owner_pk = _keys()
    _, agent_pk = _keys()

    def status(conditions: str) -> str:
        tag = json.loads(buzzkit.compute_auth_tag(owner_nsec, agent_pk, conditions))
        return buzzkit.verify_agent_profile(_profile_event(agent_pk, [tag]), owner_pk)

    assert status("") == "verified"
    assert status("kind=0&created_at>99&created_at<101") == "verified"
    assert status("kind=9") == "condition_mismatch"
    assert status("created_at<100") == "condition_mismatch"
    assert status("created_at>100") == "condition_mismatch"


def test_owner_pubkey_hex_prefers_auth_tag_owner():
    owner_nsec, owner_pk = _keys()
    agent_nsec, agent_pk = _keys()
    tag = buzzkit.compute_auth_tag(owner_nsec, agent_pk)
    assert BuzzClient("wss://x", agent_nsec, auth_tag=tag).owner_pubkey_hex == owner_pk
    assert BuzzClient("wss://x", agent_nsec).owner_pubkey_hex == agent_pk


async def test_resolve_agent_scopes_to_owner_records(monkeypatch):
    owner_nsec, owner_pk = _keys()
    agent_nsec, agent_pk = _keys()
    _, missing_pk = _keys()
    auth = json.loads(buzzkit.compute_auth_tag(owner_nsec, agent_pk, "kind=0"))

    records = [
        {"content": '{"name":"honey"}', "tags": [["d", agent_pk]]},
        {"content": '{"name":"Honey"}', "tags": [["d", missing_pk]]},
        {"content": '{"name":"Honeybee"}', "tags": [["d", "ignored"]]},
        {"content": "not json", "tags": [["d", "ignored"]]},
    ]
    profiles = [_profile_event(agent_pk, [auth])]

    async def fake_query(filters):
        kinds = filters[0]["kinds"]
        if kinds == [buzzkit.KIND_MANAGED_AGENT]:
            assert filters[0]["authors"] == [owner_pk]
            return records
        assert kinds == [0]
        return profiles

    client = BuzzClient("wss://x", agent_nsec)
    monkeypatch.setattr(client, "query", fake_query)
    result = await client.resolve_agent("Honey", owner_pk)

    by_pk = {r["pubkey"]: r for r in result}
    assert set(by_pk) == {agent_pk, missing_pk}
    assert by_pk[agent_pk]["verification"] == "verified"
    assert by_pk[agent_pk]["owner_pubkey"] == owner_pk
    assert by_pk[agent_pk]["profile"]["display_name"] == "Honey"
    assert by_pk[missing_pk]["verification"] == "missing_profile"
    assert "owner_pubkey" not in by_pk[missing_pk]
