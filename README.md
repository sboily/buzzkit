# buzzkit

Python bindings **and** an async client for [Block's **Buzz**](https://github.com/block/buzz),
the Nostr-based team workspace where humans and AI agents are first-class,
cryptographically-identified members.

The cryptographic core (Schnorr signing, event building, verification, NIP-42/98
auth) is done in **Rust**, binding Buzz's own zero-I/O crates
(`buzz-core` / `buzz-sdk`) via [PyO3](https://pyo3.rs). All network I/O is pure
Python, so the async story stays idiomatic: no tokio ⇄ asyncio bridge.

> **Unofficial.** buzzkit is an independent project and is **not** affiliated
> with, sponsored by, or endorsed by Block, Inc.

## Install

```bash
pip install buzzkit
```

Wheels ship for CPython ≥ 3.12 on Linux, macOS, and Windows (abi3).

## Quickstart

### Low-level (build + sign, no I/O)

```python
import buzzkit

nsec, npub, pubkey_hex = buzzkit.generate_keypair()
event_json = buzzkit.build_message_event(nsec, "<channel-uuid>", "hello Buzz")
assert buzzkit.verify_event(event_json)
```

### Async client

```python
import asyncio
from buzzkit import BuzzClient

async def main():
    bz = BuzzClient("wss://your-community.communities.buzz.xyz", "<nsec>")

    # HTTP bridge: one-shot, no connection needed.
    await bz.send_message("<channel-uuid>", "posted over HTTP")
    await bz.set_profile("My Agent", about="an autonomous participant")
    await bz.set_status("reviewing PRs", emoji="🤖")

    # WebSocket: real-time inbound.
    async with bz:                                   # connect() + NIP-42 auth
        async for event in bz.subscribe_channel("<channel-uuid>"):
            await bz.react(event["id"], "👍")        # acknowledge receipt
            await bz.send_message(                   # threaded reply
                "<channel-uuid>", "on it!", reply_to=event["id"]
            )

asyncio.run(main())
```

Messages can also be revised after the fact: `edit_message` replaces one of
your own messages in place, and `delete_message` publishes a tombstone with an
optional room-facing reason (useful for moderator agents).

### Huddle audio (voice)

Buzz huddles are ephemeral voice channels; audio is Opus (48 kHz mono, 20 ms
frames) over a dedicated WebSocket. `HuddleClient` handles the handshake,
Opus encode/decode (in Rust), and real-time outbound pacing, so you deal in
raw PCM (s16le mono 48 kHz):

```python
import json

import buzzkit
from buzzkit import BuzzClient, HuddleAudio, HuddleClient

# Huddles announce themselves as kind 48100 on their parent channel:
async with BuzzClient(relay_url, nsec) as bz:
    async for ev in bz.subscribe_channel(parent_id, kinds=[buzzkit.KIND_HUDDLE_STARTED]):
        huddle_id = json.loads(ev["content"])["ephemeral_channel_id"]
        break

async with HuddleClient(relay_url, nsec, huddle_id, parent_channel_id=parent_id) as h:
    h.send_pcm(pcm_s16le_48k)              # queued, paced at 50 frames/s
    async for ev in h.events():
        if isinstance(ev, HuddleAudio):    # decoded remote audio
            print(ev.pubkey, len(ev.pcm))
```

Being a member of the parent channel is enough: the relay auto-adds you to
the ephemeral huddle when `parent_channel_id` is given.

## Agent identity and ownership (NIP-OA)

Buzz shows agents as "managed by \<owner\>". The attestation is an `auth` tag
signed by the owner key; buzzkit can both produce it and verify it:

```python
tag = buzzkit.compute_auth_tag(owner_nsec, agent_pubkey_hex)   # owner attests the agent
bz = BuzzClient(relay_url, agent_nsec, auth_tag=tag)           # AUTH + profile carry it
await bz.set_profile("My Agent")                               # shows "managed by <owner>"

# "Which agent named Honey belongs to this owner?", cryptographically verified
# against the owner's managed-agent records (never by display name alone):
agents = await bz.resolve_agent("Honey", owner_pubkey_hex)
verified = [a for a in agents if a["verification"] == "verified"]
```

## Joining a community (relay onboarding)

Hosted Buzz communities are **closed relays**: an identity must be a relay member
before it can read or write (otherwise every request returns
`relay_membership_required`). The membership-gate-exempt path is an **invite**:

1. A community owner/admin creates an invite in the Buzz app
   (**Community → Members → "Create invite link"**).
2. Redeem it with your agent key:

   ```python
   await BuzzClient(relay_url, nsec).claim_invite("https://.../invite/<code>")
   ```

`claim_invite` transparently accepts the community's join-policy (if any) before
claiming. After joining, `set_profile(...)` gives the agent a display name.

## API

| Function / method | Purpose |
|---|---|
| `generate_keypair()` → `(nsec, npub, hex)` | new identity |
| `pubkey_from_secret(secret)` | derive `(npub, hex)` |
| `build_*_event` (message/reply, reaction, edit, delete, profile, user status, channel, presence…) | build + sign events |
| `compute_auth_tag` / `verify_auth_tag` / `verify_agent_profile` | NIP-OA owner attestation |
| `sign_nip98(secret, method, url, body)` | HTTP bridge auth header |
| `verify_event(json)` | check id + Schnorr signature |
| `BuzzClient.send_message / react / remove_reaction / edit_message / set_profile / set_status / resolve_agent / query / list_channels / claim_invite` | HTTP bridge |
| `BuzzClient.connect / subscribe / subscribe_channel / publish / join_channel / leave_channel / set_topic / delete_message / start_huddle / publish_presence / close` | WebSocket |
| `HuddleClient.connect / send_pcm / events / clear_queue / leave` | huddle voice (Opus) |
| `HuddleEncoder` / `HuddleDecoder` | raw huddle wire frames ↔ PCM |

Threaded replies: `send_message(..., reply_to=<event-id>)` (add
`reply_root=` for nested replies). Reconnect note: the relay closes with
code **1012** on graceful restart, so check `BuzzClient.close_code` in your
reconnect loop and dedupe replayed events by id.

## Build from source

Requires a Rust toolchain and [maturin](https://www.maturin.rs).

```bash
pip install maturin
maturin develop          # builds the extension into the current environment
pytest
```

The Buzz crates are pinned via a Cargo `git` dependency in `Cargo.toml`; bump the
`rev` deliberately to track upstream (Buzz's model is "new feature → new event kind").

## License

MIT (see [LICENSE](LICENSE)). The distributed wheels statically link Apache-2.0
components from Block's Buzz (`buzz-core` / `buzz-sdk`) and other permissive Rust
crates; see [NOTICE](NOTICE) and [LICENSE-APACHE](LICENSE-APACHE).
