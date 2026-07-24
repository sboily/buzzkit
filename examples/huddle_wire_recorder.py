"""Wire-side recorder for Buzz huddles — capture exactly what receivers get.

Joins a huddle as a silent peer (sends nothing) and records every inbound
audio frame with its arrival time and wire header. This is the ground truth
for "what does the desktop app's jitter buffer actually see": if the audio
is already choppy here, the sender (e.g. the roomkit agent) is at fault; if
it is clean here but choppy in the app, the receiver side is at fault.

Watch mode (recommended): give it only the parent channel — the one in your
BUZZ_CHANNEL_ID — and it records every huddle announced there (kind 48100),
one output directory per huddle:

    BUZZ_RELAY_URL=wss://... python examples/huddle_wire_recorder.py \
        <parent_channel_id>

Direct mode, when you already know the ephemeral huddle id (the agent logs
it as "Joined huddle <uuid>"):

    python examples/huddle_wire_recorder.py <parent_channel_id> \
        <huddle_channel_id> [out_dir]

Output (out_dir, default ./wire-tap-<huddle>):
    recv_frames.csv    one row per wire frame:
                       t_mono_ns,peer_index,seq,ts_48k,level_dbov,is_dtx,pcm_bytes
    events.csv         roster events: t_mono_ns,event,pubkey
    peer<N>_48k.raw    decoded s16le mono 48 kHz PCM per peer (non-DTX frames,
                       concatenated in arrival order — listen with:
                       ffplay -f s16le -ar 48000 -ch_layout mono peer<N>_48k.raw)

Analyze the capture with examples/analyze_audio_tap.py.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import time

from _shared import agent_secret, relay_url
from buzzkit import KIND_HUDDLE_STARTED, BuzzClient, HuddleAudio, HuddleClient


async def record(relay: str, secret: str, huddle_id: str, parent_id: str, out: pathlib.Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    frames_csv = (out / "recv_frames.csv").open("w")
    frames_csv.write("t_mono_ns,peer_index,seq,ts_48k,level_dbov,is_dtx,pcm_bytes\n")
    events_csv = (out / "events.csv").open("w")
    events_csv.write("t_mono_ns,event,pubkey\n")
    pcm_files: dict[int, object] = {}
    n_frames = 0

    # paced=False: the client never sends anything on its own (no silence
    # stream), so this peer is acoustically invisible in the huddle.
    client = HuddleClient(relay, secret, huddle_id, parent_channel_id=parent_id, paced=False)
    async with client:
        print(f"recording huddle {huddle_id} -> {out} (Ctrl+C to stop)")
        try:
            async for ev in client.events():
                now = time.monotonic_ns()
                if isinstance(ev, HuddleAudio):
                    frames_csv.write(
                        f"{now},{ev.peer_index},{ev.seq},{ev.ts_48k},"
                        f"{ev.level_dbov},{int(ev.is_dtx)},{len(ev.pcm)}\n"
                    )
                    if not ev.is_dtx:
                        f = pcm_files.get(ev.peer_index)
                        if f is None:
                            f = (out / f"peer{ev.peer_index}_48k.raw").open("wb")
                            pcm_files[ev.peer_index] = f
                        f.write(ev.pcm)
                    n_frames += 1
                    if n_frames % 500 == 0:
                        frames_csv.flush()
                        print(f"  {n_frames} frames…")
                else:
                    events_csv.write(f"{now},{type(ev).__name__},{ev.pubkey}\n")
                    events_csv.flush()
        finally:
            frames_csv.close()
            events_csv.close()
            for f in pcm_files.values():
                f.close()
            print(f"done: {n_frames} frames from {len(pcm_files)} speaking peer(s)")
            print(f"analyze with: python examples/analyze_audio_tap.py {out}")


async def watch_and_record(relay: str, secret: str, parent_id: str) -> None:
    """Record every huddle announced on the parent channel from now on."""
    print(f"watching {parent_id} for huddles (kind {KIND_HUDDLE_STARTED})…")
    async with BuzzClient(relay, secret) as bz:
        live = {"kinds": [KIND_HUDDLE_STARTED], "#h": [parent_id], "since": int(time.time())}
        async for event in bz.subscribe([live]):
            huddle_id = json.loads(event["content"]).get("ephemeral_channel_id")
            if not huddle_id:
                continue
            out = pathlib.Path(f"wire-tap-{huddle_id[:8]}")
            await record(relay, secret, huddle_id, parent_id, out)
            print(f"watching {parent_id} for the next huddle…")


async def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(
            "usage: huddle_wire_recorder.py <parent_channel_id> [huddle_channel_id] [out_dir]"
        )
    parent_id = sys.argv[1]
    if len(sys.argv) < 3:
        await watch_and_record(relay_url(), agent_secret(), parent_id)
        return
    huddle_id = sys.argv[2]
    out = pathlib.Path(sys.argv[3] if len(sys.argv) > 3 else f"wire-tap-{huddle_id[:8]}")
    await record(relay_url(), agent_secret(), huddle_id, parent_id, out)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
