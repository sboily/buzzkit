# Changelog

All notable changes to buzzkit are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.3.0] — 2026-08-02

### Added

- **Owner control commands**: `parse_owner_command(event, agent_pubkey_hex)`
  matches the platform's owner commands (`!shutdown` / `!cancel` / `!rotate`)
  on the wire — kind 9, exact trimmed content, `p`-tag mention of the agent —
  mirroring upstream `buzz-acp`'s `is_owner_control_command` (`!shutdown
  please` is a regular message, ` !rotate ` is a command). Pure function, no
  I/O. `OWNER_COMMANDS` exported.
- **`BuzzClient.verified_owner_hex`**: the NIP-OA auth tag's attester,
  Schnorr-verified against the client's own pubkey at construction; `None`
  when the tag is absent or does not verify. This is the value that must gate
  privileged actions (e.g. honoring `!shutdown`).

### Documented

- `owner_pubkey_hex` is **declared, not proven** — it reads the tag's owner
  field without verifying the signature and must never gate privileged
  actions. The README's NIP-OA section shows both halves of the
  owner-command check.

## [0.2.1] — 2026-07-31

### Fixed

- **HTTP bridge errors are now readable**: a rejected bridge call previously
  raised a bare `400 Bad Request`; the relay's actual reason (e.g.
  `invalid: edit target event not found`) sat unread in the response body.
  `post_event`, `query`, and `claim_invite` now raise a `RuntimeError`
  carrying the status and body.

### Documented (relay contracts, verified live)

- `delete_message(reason=...)` is **moderation-only**: a kind-9005 tombstone
  carrying `public_reason` requires channel owner/admin rights and is
  rejected even for the message's own author. Omit `reason` to delete your
  own message.
- **The bridge acks before indexing**: an edit/delete sent immediately after
  its target can transiently fail with `target event not found`; retrying is
  the caller's call.

## [0.2.0] — 2026-07-31

### Added

- **Message lifecycle**: threaded replies
  (`send_message(..., reply_to=, reply_root=)`), `react` / `remove_reaction`
  (kinds 7/5), `edit_message` (kind 40003), `delete_message` (kind 9005
  tombstone, optional room-facing reason), `set_topic` (kind 9002),
  `leave_channel` (kind 9022).
- **Agent ownership (NIP-OA)**: `verify_auth_tag`, `verify_agent_profile`,
  and `BuzzClient.resolve_agent(name, owner)` — agent-by-name lookup scoped
  to the owner's kind-30177 managed-agent records, verified
  cryptographically (mirrors block/buzz#3178).
- **NIP-38 user status**: `build_user_status_event` / `BuzzClient.set_status`
  (kind 30315; blank text clears it).
- **Reconnect signals**: `BuzzClient.close_code` exposes the WebSocket close
  code; 1012 means the relay is restarting gracefully — back off gently and
  dedupe replayed events by id.

### Changed

- Upstream `buzz-core`/`buzz-sdk` pin moves from v0.4.23 to post-v0.5.3
  (`209536ade`); crate API delta is purely additive and picks up the IPv6
  SSRF hardening (block/buzz#2801) and kinds 30177/30178.
- `__version__` derives from package metadata instead of a hardcoded string.
- Behavior notes when upgrading: channel names are canonicalized upstream
  (leading `#` stripped; a name canonicalizing to empty raises `ValueError`);
  presence contract on buzz v0.5.x relays is a 60 s heartbeat against a
  180 s TTL (30 s stays safe on both relay generations); relay REQ pages cap
  at 1000 events and the advertised limit is now honest.

### Security

- `nostr` 0.44.4 → 0.44.6, fixing RUSTSEC-2026-0216 (remote DoS via a
  malformed NIP-44 payload; buzzkit enables `nip44`).

## [0.1.4] — 2026-07-23

### Changed

- **Huddle audio robustness**: all WebSocket I/O runs on a dedicated thread
  (caller event-loop stalls can no longer distort pacing); the paced sender
  never bursts (late frames are dropped and the clock realigned); silence is
  streamed between utterances so receiver jitter buffers see a continuous
  real-time timeline (Opus DTX keeps those packets tiny); Opus encode/decode
  release the GIL.

### Added

- `paced=False` mode: an external pacer (e.g. RoomKit's
  `OutboundAudioPacer`) owns the clock and the client just relays frames.
- Diagnostics examples: `huddle_wire_recorder.py` (join as a silent peer and
  record exactly what receivers get) and `analyze_audio_tap.py` (stage-by-
  stage timing analysis). These traced choppy huddle audio to a Buzz desktop
  playout bug (block/buzz#2652).
- CI enforces `ruff format` and `ty` type checking.

## [0.1.3] — 2026-07-23

### Added

- **Huddle voice**: `HuddleClient` (challenge → NIP-42 auth → joined
  handshake, real-time paced Opus send, roster tracking, barge-in), Rust
  `HuddleEncoder`/`HuddleDecoder` for the huddle v2 wire protocol (Opus
  48 kHz mono + 8-byte header), `BuzzClient.start_huddle()`, kind 9007/48100
  builders, and `set_profile(auth_tag=)` defaulting to the client's NIP-OA
  attestation.
- Self-contained wheels (static libopus) for Linux x86_64/aarch64
  (manylinux_2_28), macOS x86_64/arm64, and Windows x64.

## [0.1.2] — 2026-07-22

### Added

- **NIP-OA owner attestation**: `compute_auth_tag(owner_secret,
  agent_pubkey_hex, conditions)` (the owner attests the agent;
  self-attestation rejected), `build_auth_event(..., auth_tag=)`, and
  `BuzzClient(auth_tag=)` injecting the tag into AUTH and the profile.
- **Presence**: `build_presence_event` / `BuzzClient.publish_presence`
  (kind 20001, ephemeral, WebSocket-published).

## [0.1.1] — 2026-07-22

### Added

- **Channel self-join**: `join_channel` / `build_join_channel_event`
  (NIP-29 kind 9000, role `bot`). Builders allow self-tagging — `nostr`'s
  `EventBuilder` otherwise strips a `p` tag naming the event author, which
  made relays reject the self-join for a missing `p` tag.

## [0.1.0] — 2026-07-22

### Added

- Initial release. PyO3 bindings over Block's zero-I/O `buzz-core` /
  `buzz-sdk` crates: keypair generation, event building/signing/verification,
  NIP-42 auth events, NIP-98 HTTP auth — all synchronous, no tokio.
- Async `BuzzClient` (all I/O in Python): HTTP bridge (`query`,
  `list_channels`, `send_message`, `set_profile`, `claim_invite`,
  `post_event`) and authenticated WebSocket (`connect`, `subscribe`,
  `subscribe_channel`, `publish`, `close`).
- One abi3 (CPython ≥ 3.12) mixed wheel; MIT-licensed with Apache-2.0
  attribution (NOTICE) for the statically linked buzz crates.
