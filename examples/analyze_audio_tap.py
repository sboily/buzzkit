"""Analyze audio tap captures and point at the failing link.

Reads a capture directory produced by either (or both):
  * roomkit's ``examples/buzz_voice_agent.py`` run with ``BUZZ_TAP_DIR=...``
    (sender-side taps: provider_out_24k.raw, pacer_in_48k.raw,
    wire_out_48k.raw, wire_send.csv, events.csv), or
  * ``examples/huddle_wire_recorder.py`` (receiver-side: recv_frames.csv,
    peer<N>_48k.raw).

and reports, per stage: send/arrival timing (stalls, bursts), silence frames
spliced into speech, wire sequence gaps, and audible holes inside the PCM.

    python examples/analyze_audio_tap.py <capture_dir>

Reading the verdict:
  * holes in wire_out_48k.raw but not in pacer_in_48k.raw
        -> the pacer inserted silence / dropped audio (sender side).
  * clean wire_out but recv_frames.csv shows seq gaps or heavy arrival jitter
        -> frames lost or bunched between agent and relay fan-out.
  * clean + smooth arrivals at the recorder, but the app still sounds choppy
        -> receiver side (NetEq/playout in the desktop app).
"""

from __future__ import annotations

import csv
import pathlib
import sys

FRAME_MS = 20.0
NS = 1_000_000  # ns per ms


def fmt_ts(ms: float) -> str:
    return f"{int(ms // 60000):02d}:{ms % 60000 / 1000:06.3f}"


# ---------------------------------------------------------------- timing CSVs


def analyze_send_timing(rows: list[dict], t_key: str, label: str) -> None:
    """Inter-send/arrival timing: stalls (>2x frame) and bursts (<2 ms)."""
    times = [int(r[t_key]) for r in rows]
    if len(times) < 2:
        print(f"  {label}: not enough rows")
        return
    deltas = [(b - a) / NS for a, b in zip(times, times[1:])]
    span_s = (times[-1] - times[0]) / NS / 1000
    stalls = [(i, d) for i, d in enumerate(deltas) if d > 2 * FRAME_MS]
    bursts = sum(1 for d in deltas if d < 2.0)
    print(
        f"  {label}: {len(times)} frames over {span_s:.1f}s "
        f"(mean {sum(deltas) / len(deltas):.1f} ms/frame, max gap {max(deltas):.0f} ms)"
    )
    print(f"    gaps >{2 * FRAME_MS:.0f} ms: {len(stalls)}   sends <2 ms apart (bursts): {bursts}")
    t0 = times[0]
    for i, d in stalls[:10]:
        print(f"      gap {d:6.0f} ms at t+{fmt_ts((times[i] - t0) / NS)}")
    if len(stalls) > 10:
        print(f"      … and {len(stalls) - 10} more")


def analyze_wire_csv(path: pathlib.Path) -> None:
    rows = list(csv.DictReader(path.open()))
    if not rows:
        print("  wire_send.csv: empty")
        return
    print("wire_send.csv — pacer output timing (what buzzkit is asked to send):")
    analyze_send_timing(rows, "t_mono_ns", "pacer->send_pcm")

    # Silence frames spliced into speech: an all-zero send whose neighbours
    # within 300 ms on both sides contain non-zero audio.
    times = [int(r["t_mono_ns"]) for r in rows]
    zero = [r["all_zero"] == "1" for r in rows]
    t0 = times[0]
    nonzero_times = [t for t, z in zip(times, zero) if not z]
    spliced = []
    for t, z in zip(times, zero):
        if not z or not nonzero_times:
            continue
        # nearest real-audio sends before/after this silence frame
        before = any(0 < t - u <= 300 * NS for u in nonzero_times)
        after = any(0 < u - t <= 300 * NS for u in nonzero_times)
        if before and after:
            spliced.append(t)
    n_zero = sum(zero)
    print(f"    all-zero frames: {n_zero} total, {len(spliced)} spliced mid-speech")
    for t in spliced[:10]:
        print(f"      silence splice at t+{fmt_ts((t - t0) / NS)}")
    if len(spliced) > 10:
        print(f"      … and {len(spliced) - 10} more")


def analyze_recv_csv(path: pathlib.Path) -> None:
    rows = list(csv.DictReader(path.open()))
    if not rows:
        print("  recv_frames.csv: empty")
        return
    print("recv_frames.csv — wire arrivals at the recorder (per peer):")
    peers: dict[str, list[dict]] = {}
    for r in rows:
        peers.setdefault(r["peer_index"], []).append(r)
    for peer, frames in sorted(peers.items()):
        speech = [f for f in frames if f["is_dtx"] == "0"]
        dtx = len(frames) - len(speech)
        print(f"  peer {peer}: {len(speech)} speech frames, {dtx} DTX")
        if len(speech) < 2:
            continue
        analyze_send_timing(speech, "t_mono_ns", f"peer {peer} arrivals")
        # Wire continuity over ALL frames (DTX included — every wire packet
        # consumes one seq and 960 ts): dseq != 1 means frames were lost or
        # reordered between the sender and us; dts != dseq*960 means the
        # sender's own media timeline jumped (encoder restart / bug).
        lost = jumps = 0
        for a, b in zip(frames, frames[1:]):
            dseq = (int(b["seq"]) - int(a["seq"])) % 65536
            dts = (int(b["ts_48k"]) - int(a["ts_48k"])) % (1 << 32)
            if dseq != 1:
                lost += 1
                if lost <= 10:
                    print(f"      lost/reordered: seq {a['seq']}->{b['seq']} ({dseq - 1} missing)")
            elif dts != 960:
                jumps += 1
                if jumps <= 10:
                    print(f"      sender ts jump at seq {b['seq']}: +{dts} (expected +960)")
        print(f"    lost/reordered frames: {lost}   sender ts jumps: {jumps}")


# ------------------------------------------------------------------ PCM scans


def analyze_raw(path: pathlib.Path, rate: int) -> None:
    """Find near-silent runs >=15 ms that sit inside speech (audible holes)."""
    data = path.read_bytes()
    samples = memoryview(data).cast("h")
    n = len(samples)
    if n == 0:
        print(f"  {path.name}: empty")
        return
    ms = 1000.0 / rate
    win = rate // 100  # 10 ms windows
    # Classify 10 ms windows as silent/speech, then find silent runs
    # bordered by speech within 200 ms on each side.
    n_win = n // win
    loud = []
    for w in range(n_win):
        seg = samples[w * win : (w + 1) * win]
        peak = max(abs(min(seg)), abs(max(seg)))
        loud.append(peak > 400)
    holes: list[tuple[int, int]] = []
    w = 0
    while w < n_win:
        if loud[w]:
            w += 1
            continue
        start = w
        while w < n_win and not loud[w]:
            w += 1
        run_ms = (w - start) * 10
        ctx = 20  # 200 ms of context windows
        speech_before = any(loud[max(0, start - ctx) : start])
        speech_after = any(loud[w : w + ctx])
        if run_ms >= 15 and run_ms <= 2000 and speech_before and speech_after:
            holes.append((start, run_ms))
    dur_s = n * ms / 1000
    print(f"  {path.name}: {dur_s:.1f}s of audio, {len(holes)} hole(s) inside speech")
    for start, run_ms in holes[:15]:
        print(f"      {run_ms:4d} ms hole at {fmt_ts(start * 10.0)}")
    if len(holes) > 15:
        print(f"      … and {len(holes) - 15} more")


RAW_RATES = {"24k": 24_000, "48k": 48_000, "16k": 16_000}


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: analyze_audio_tap.py <capture_dir>")
    cap = pathlib.Path(sys.argv[1])
    if not cap.is_dir():
        raise SystemExit(f"not a directory: {cap}")

    found = False
    wire_csv = cap / "wire_send.csv"
    if wire_csv.exists():
        found = True
        analyze_wire_csv(wire_csv)
        print()
    recv_csv = cap / "recv_frames.csv"
    if recv_csv.exists():
        found = True
        analyze_recv_csv(recv_csv)
        print()
    events_csv = cap / "events.csv"
    if events_csv.exists():
        rows = list(csv.DictReader(events_csv.open()))
        interesting = [r for r in rows if r.get("event") not in (None, "")]
        if interesting:
            print(f"events.csv: {len(interesting)} event(s)")
            for r in interesting[:20]:
                print(f"  {r}")
            print()
    raws = sorted(cap.glob("*.raw"))
    if raws:
        found = True
        print("PCM hole scan (near-silence >=15 ms surrounded by speech):")
        for raw in raws:
            rate = next(
                (hz for tag, hz in RAW_RATES.items() if tag in raw.name),
                48_000,
            )
            analyze_raw(raw, rate)
    if not found:
        raise SystemExit(f"no tap files found in {cap}")


if __name__ == "__main__":
    main()
