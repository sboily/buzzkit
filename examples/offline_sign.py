"""Offline proof — no relay. keygen -> sign a Buzz message -> verify -> tamper check.

    python examples/offline_sign.py
"""

from __future__ import annotations

import json
import uuid

import buzzkit


def main() -> None:
    nsec, npub, pk_hex = buzzkit.generate_keypair()
    print(f"agent npub: {npub}")

    channel_id = str(uuid.uuid4())
    event_json = buzzkit.build_message_event(nsec, channel_id, "hello Buzz from Python \U0001f41d")
    event = json.loads(event_json)
    print(json.dumps(event, indent=2))

    assert event["kind"] == buzzkit.KIND_STREAM_MESSAGE
    assert event["pubkey"] == pk_hex
    assert ["h", channel_id] in event["tags"]
    assert buzzkit.verify_event(event_json)
    assert not buzzkit.verify_event(json.dumps(dict(event, content="tampered")))

    print("\nOK: sign + verify round-trip, tamper detected.")


if __name__ == "__main__":
    main()
