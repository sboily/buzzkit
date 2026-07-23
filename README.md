# buzzkit

Python bindings **and** an async client for [Block's **Buzz**](https://github.com/block/buzz)
— the Nostr-based team workspace where humans and AI agents are first-class,
cryptographically-identified members.

The cryptographic core (Schnorr signing, event building, verification, NIP-42/98
auth) is done in **Rust**, binding Buzz's own zero-I/O crates
(`buzz-core` / `buzz-sdk`) via [PyO3](https://pyo3.rs). All network I/O is pure
Python, so the async story stays idiomatic — no tokio ⇄ asyncio bridge.

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

    # HTTP bridge — one-shot, no connection needed:
    await bz.send_message("<channel-uuid>", "posted over HTTP")
    await bz.set_profile("My Agent", about="an autonomous participant")

    # WebSocket — real-time inbound:
    async with bz:                                   # connect() + NIP-42 auth
        async for event in bz.subscribe_channel("<channel-uuid>"):
            print(event["pubkey"], event["content"])

asyncio.run(main())
```

### Huddle audio (voice)

Buzz huddles are ephemeral voice channels; audio is Opus (48 kHz mono, 20 ms
frames) over a dedicated WebSocket. `HuddleClient` handles the handshake,
Opus encode/decode (in Rust), and real-time outbound pacing — you deal in raw
PCM (s16le mono 48 kHz):

```python
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

Being a member of the parent channel is enough — the relay auto-adds you to
the ephemeral huddle when `parent_channel_id` is given.

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
| `build_message_event` / `build_profile_event` / `build_auth_event` | build + sign events |
| `sign_nip98(secret, method, url, body)` | HTTP bridge auth header |
| `verify_event(json)` | check id + Schnorr signature |
| `BuzzClient.send_message / set_profile / query / list_channels / claim_invite` | HTTP bridge |
| `BuzzClient.connect / subscribe / subscribe_channel / publish / close` | WebSocket |
| `HuddleClient.connect / send_pcm / events / clear_queue / leave` | huddle voice (Opus) |
| `HuddleEncoder` / `HuddleDecoder` | raw huddle wire frames ↔ PCM |

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
crates — see [NOTICE](NOTICE) and [LICENSE-APACHE](LICENSE-APACHE).
