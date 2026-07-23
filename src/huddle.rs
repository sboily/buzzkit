//! Huddle audio: v2 wire protocol + Opus codec state.
//!
//! Mirrors Buzz's wire format (relay `buzz-relay/src/audio/wire.rs`, desktop
//! `desktop/src-tauri/src/huddle/wire.rs`), which is not exported as a
//! reusable crate:
//!
//! ```text
//! client → relay: <header: [u8; 8]><opus_bytes>
//! relay → client: <peer_index: u8><header: [u8; 8]><opus_bytes>
//!
//! header (big-endian):
//!   byte 0..=1 : seq         u16  wrapping, +1 per packet
//!   byte 2..=5 : ts_48k      u32  48 kHz media time, +960 per 20 ms frame
//!   byte 6     : level_dbov  i8   RMS level in dBov, range [-127, 0]
//!   byte 7     : flags       u8   bit 0 = DTX/comfort frame
//! ```
//!
//! Audio is Opus, 48 kHz mono, 20 ms frames (960 samples). The PCM boundary
//! on the Python side is s16le mono 48 kHz — resampling to/from a voice
//! provider's rate is the caller's concern (RoomKit's RealtimeVoiceChannel
//! resamples when given `transport_sample_rate=48000`).

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::collections::HashMap;
use std::sync::Mutex;

/// Huddle audio protocol version this client speaks (sent in the WS auth
/// message; the relay pins a room to its first joiner's version).
pub const PROTOCOL_VERSION: u8 = 2;

/// Length of the v2 per-frame header in bytes.
pub const V2_HEADER_LEN: usize = 8;

/// `flags` bit 0 — DTX/comfort-noise frame.
pub const FLAG_DTX: u8 = 0x01;

/// Huddle audio sample rate (Hz). Fixed by the protocol.
pub const SAMPLE_RATE: u32 = 48_000;

/// Samples per 20 ms Opus frame at 48 kHz mono.
pub const FRAME_SAMPLES: usize = 960;

/// Largest possible Opus frame at 48 kHz: 120 ms = 5760 samples.
const MAX_DECODE_SAMPLES: usize = 5760;

/// Opus DTX comfort packets are 1-2 bytes; anything that small is flagged DTX
/// on the wire (same heuristic as the Buzz desktop encoder).
const DTX_MAX_PAYLOAD: usize = 2;

/// RMS level of an s16 frame in dBov, clamped to the header's [-127, 0] range.
fn audio_level_dbov(samples: &[i16]) -> i8 {
    if samples.is_empty() {
        return -127;
    }
    let mean_square: f64 = samples
        .iter()
        .map(|&s| {
            let v = f64::from(s) / 32768.0;
            v * v
        })
        .sum::<f64>()
        / samples.len() as f64;
    if mean_square <= 0.0 {
        return -127;
    }
    let db = 10.0 * mean_square.log10();
    if !db.is_finite() || db <= -127.0 {
        -127
    } else if db >= 0.0 {
        0
    } else {
        db.round() as i8
    }
}

fn encode_header(seq: u16, ts_48k: u32, level_dbov: i8, flags: u8) -> [u8; V2_HEADER_LEN] {
    let mut out = [0u8; V2_HEADER_LEN];
    out[0..2].copy_from_slice(&seq.to_be_bytes());
    out[2..6].copy_from_slice(&ts_48k.to_be_bytes());
    out[6] = level_dbov as u8;
    out[7] = flags;
    out
}

/// Parsed inbound frame: peer index, header fields, and the Opus payload.
type ParsedInbound<'a> = (u8, u16, u32, i8, u8, &'a [u8]);

/// Decoded inbound frame: peer index, header fields, and the PCM sample count.
type DecodedInbound = (u8, u16, u32, i8, u8, usize);

fn parse_inbound(frame: &[u8]) -> Result<ParsedInbound<'_>, String> {
    // 1-byte peer_index + 8-byte header + at least 1 byte of Opus payload.
    if frame.len() <= 1 + V2_HEADER_LEN {
        return Err(format!(
            "frame too short: {} bytes (need > {})",
            frame.len(),
            1 + V2_HEADER_LEN
        ));
    }
    let peer_index = frame[0];
    let h = &frame[1..];
    let seq = u16::from_be_bytes([h[0], h[1]]);
    let ts_48k = u32::from_be_bytes([h[2], h[3], h[4], h[5]]);
    let raw_level = h[6] as i8;
    // Out-of-range telemetry clamps to the silence floor; never drop audio
    // over bad VU metadata (same invariant as the relay).
    let level_dbov = if (-127..=0).contains(&raw_level) {
        raw_level
    } else {
        -127
    };
    let flags = h[7];
    Ok((
        peer_index,
        seq,
        ts_48k,
        level_dbov,
        flags,
        &h[V2_HEADER_LEN..],
    ))
}

fn pcm_bytes_to_i16(pcm: &[u8]) -> Result<Vec<i16>, String> {
    if !pcm.len().is_multiple_of(2) {
        return Err(format!(
            "PCM byte length must be even (s16le), got {}",
            pcm.len()
        ));
    }
    Ok(pcm
        .chunks_exact(2)
        .map(|b| i16::from_le_bytes([b[0], b[1]]))
        .collect())
}

/// Stateful huddle audio encoder: s16le mono 48 kHz PCM in, v2 wire frames out.
///
/// Keeps the Opus encoder, sequence/timestamp counters, and a carry-over
/// buffer for partial 20 ms frames across calls. One instance per huddle
/// connection. The interior mutex exists because PyO3 requires `Sync`
/// (the raw libopus handle is not); contention is nil in practice.
#[pyclass]
pub struct HuddleEncoder {
    inner: Mutex<EncoderInner>,
}

struct EncoderInner {
    encoder: opus::Encoder,
    carry: Vec<i16>,
    seq: u16,
    ts_48k: u32,
    scratch: Vec<u8>,
}

impl EncoderInner {
    fn new(bitrate: i32, dtx: bool) -> Result<Self, String> {
        let mut encoder =
            opus::Encoder::new(SAMPLE_RATE, opus::Channels::Mono, opus::Application::Voip)
                .map_err(|e| format!("opus encoder: {e}"))?;
        encoder
            .set_bitrate(opus::Bitrate::Bits(bitrate))
            .map_err(|e| format!("opus bitrate: {e}"))?;
        encoder.set_dtx(dtx).map_err(|e| format!("opus dtx: {e}"))?;
        Ok(Self {
            encoder,
            carry: Vec::new(),
            seq: 0,
            ts_48k: 0,
            scratch: vec![0u8; 4000],
        })
    }

    fn encode_frame(&mut self, chunk: &[i16]) -> PyResult<Option<Vec<u8>>> {
        debug_assert_eq!(chunk.len(), FRAME_SAMPLES);
        // Level comes from the pre-encode PCM: a DTX comfort packet's payload
        // would give a meaningless RMS.
        let level = audio_level_dbov(chunk);
        let n = self
            .encoder
            .encode(chunk, &mut self.scratch)
            .map_err(|e| PyValueError::new_err(format!("opus encode: {e}")))?;
        if n == 0 {
            return Ok(None);
        }
        let flags = if n <= DTX_MAX_PAYLOAD { FLAG_DTX } else { 0 };
        let mut frame = Vec::with_capacity(V2_HEADER_LEN + n);
        frame.extend_from_slice(&encode_header(self.seq, self.ts_48k, level, flags));
        frame.extend_from_slice(&self.scratch[..n]);
        self.seq = self.seq.wrapping_add(1);
        self.ts_48k = self.ts_48k.wrapping_add(FRAME_SAMPLES as u32);
        Ok(Some(frame))
    }

    fn drain(&mut self, pcm: &[u8]) -> PyResult<Vec<Vec<u8>>> {
        let samples = pcm_bytes_to_i16(pcm).map_err(PyValueError::new_err)?;
        self.carry.extend_from_slice(&samples);
        let mut frames = Vec::new();
        let mut offset = 0;
        while self.carry.len() - offset >= FRAME_SAMPLES {
            let chunk: Vec<i16> = self.carry[offset..offset + FRAME_SAMPLES].to_vec();
            if let Some(frame) = self.encode_frame(&chunk)? {
                frames.push(frame);
            }
            offset += FRAME_SAMPLES;
        }
        self.carry.drain(..offset);
        Ok(frames)
    }
}

#[pymethods]
impl HuddleEncoder {
    /// `bitrate` in bits/s and `dtx` mirror the Buzz desktop defaults
    /// (32 kbps VOIP mono, DTX on).
    #[new]
    #[pyo3(signature = (bitrate=32_000, dtx=true))]
    fn new(bitrate: i32, dtx: bool) -> PyResult<Self> {
        Ok(Self {
            inner: Mutex::new(EncoderInner::new(bitrate, dtx).map_err(PyValueError::new_err)?),
        })
    }

    /// Feed s16le mono 48 kHz PCM bytes; returns zero or more complete v2
    /// wire frames, each ready to send as one WS binary message. Trailing
    /// samples short of a 20 ms frame are buffered for the next call.
    fn encode(&self, py: Python<'_>, pcm: &[u8]) -> PyResult<Vec<Py<PyBytes>>> {
        let frames = self.lock().drain(pcm)?;
        Ok(frames
            .into_iter()
            .map(|f| PyBytes::new(py, &f).into())
            .collect())
    }

    /// Zero-pad and emit the buffered partial frame, if any. Call at the end
    /// of an utterance so its tail is not held back.
    fn flush(&self, py: Python<'_>) -> PyResult<Option<Py<PyBytes>>> {
        let mut inner = self.lock();
        if inner.carry.is_empty() {
            return Ok(None);
        }
        let mut chunk = std::mem::take(&mut inner.carry);
        chunk.resize(FRAME_SAMPLES, 0);
        Ok(inner
            .encode_frame(&chunk)?
            .map(|f| PyBytes::new(py, &f).into()))
    }

    /// Drop buffered PCM without emitting it (barge-in: the rest of the
    /// utterance was cancelled). Counters keep advancing per sent frame.
    fn discard(&self) {
        self.lock().carry.clear();
    }
}

impl HuddleEncoder {
    fn lock(&self) -> std::sync::MutexGuard<'_, EncoderInner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }
}

/// Stateful huddle audio decoder for relay frames
/// (`[peer_index u8][header 8B][opus]`).
///
/// Opus decoders are stateful per stream, so one is kept per `peer_index`.
/// Call `remove_peer` when a peer leaves — the relay recycles indexes, and a
/// new peer must not inherit a predecessor's decoder state.
#[pyclass]
pub struct HuddleDecoder {
    inner: Mutex<DecoderInner>,
}

struct DecoderInner {
    decoders: HashMap<u8, opus::Decoder>,
    scratch: Vec<i16>,
}

impl DecoderInner {
    fn decode(&mut self, frame: &[u8]) -> Result<DecodedInbound, String> {
        let (peer_index, seq, ts_48k, level_dbov, flags, payload) = parse_inbound(frame)?;
        let decoder = match self.decoders.entry(peer_index) {
            std::collections::hash_map::Entry::Occupied(e) => e.into_mut(),
            std::collections::hash_map::Entry::Vacant(e) => e.insert(
                opus::Decoder::new(SAMPLE_RATE, opus::Channels::Mono)
                    .map_err(|e| format!("opus decoder: {e}"))?,
            ),
        };
        let n = decoder
            .decode(payload, &mut self.scratch, false)
            .map_err(|e| format!("opus decode: {e}"))?;
        Ok((peer_index, seq, ts_48k, level_dbov, flags, n))
    }
}

#[pymethods]
impl HuddleDecoder {
    #[new]
    fn new() -> Self {
        Self {
            inner: Mutex::new(DecoderInner {
                decoders: HashMap::new(),
                scratch: vec![0i16; MAX_DECODE_SAMPLES],
            }),
        }
    }

    /// Decode one inbound relay frame. Returns
    /// `(peer_index, seq, ts_48k, level_dbov, is_dtx, pcm)` where `pcm` is
    /// s16le mono 48 kHz bytes. Raises ValueError on a malformed frame.
    fn decode(
        &self,
        py: Python<'_>,
        frame: &[u8],
    ) -> PyResult<(u8, u16, u32, i8, bool, Py<PyBytes>)> {
        let mut inner = self.lock();
        let (peer_index, seq, ts_48k, level_dbov, flags, n) =
            inner.decode(frame).map_err(PyValueError::new_err)?;
        let pcm: Vec<u8> = inner.scratch[..n]
            .iter()
            .flat_map(|s| s.to_le_bytes())
            .collect();
        Ok((
            peer_index,
            seq,
            ts_48k,
            level_dbov,
            flags & FLAG_DTX != 0,
            PyBytes::new(py, &pcm).into(),
        ))
    }

    /// Forget a peer's decoder state (call on "left"; indexes are recycled).
    fn remove_peer(&self, peer_index: u8) {
        self.lock().decoders.remove(&peer_index);
    }
}

impl HuddleDecoder {
    fn lock(&self) -> std::sync::MutexGuard<'_, DecoderInner> {
        self.inner.lock().unwrap_or_else(|e| e.into_inner())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Byte layout pinned against Buzz's own wire tests: BE u16 seq,
    /// BE u32 ts_48k, i8 level, u8 flags.
    #[test]
    fn header_is_network_byte_order() {
        let bytes = encode_header(0x0102, 0x0304_0506, -1, 0);
        assert_eq!(bytes[0..2], [0x01, 0x02]);
        assert_eq!(bytes[2..6], [0x03, 0x04, 0x05, 0x06]);
        assert_eq!(bytes[6], 0xFF); // -1 as i8
        assert_eq!(bytes[7], 0x00);
    }

    #[test]
    fn parse_inbound_reads_prefix_and_header() {
        let mut frame = vec![7u8]; // peer_index
        frame.extend_from_slice(&encode_header(0x0102, 0x0304_0506, -40, FLAG_DTX));
        frame.extend_from_slice(b"opus");
        let (idx, seq, ts, level, flags, payload) = parse_inbound(&frame).expect("parse");
        assert_eq!(idx, 7);
        assert_eq!(seq, 0x0102);
        assert_eq!(ts, 0x0304_0506);
        assert_eq!(level, -40);
        assert_eq!(flags & FLAG_DTX, FLAG_DTX);
        assert_eq!(payload, b"opus");
    }

    #[test]
    fn parse_inbound_rejects_short_frames() {
        // Prefix + header with no payload must be rejected, like the relay.
        for len in 0..=(1 + V2_HEADER_LEN) {
            assert!(parse_inbound(&vec![0u8; len]).is_err(), "{len} bytes");
        }
    }

    #[test]
    fn parse_inbound_clamps_bad_level_keeps_frame() {
        let mut frame = vec![0u8];
        let mut header = encode_header(7, 960, 0, 0);
        header[6] = 0x7F; // +127 — out of the canonical [-127, 0] range
        frame.extend_from_slice(&header);
        frame.extend_from_slice(b"x");
        let (_, seq, _, level, _, payload) = parse_inbound(&frame).expect("parse");
        assert_eq!(level, -127, "invalid level clamps to silence floor");
        assert_eq!(seq, 7);
        assert_eq!(payload, b"x");
    }

    #[test]
    fn level_dbov_bounds() {
        assert_eq!(audio_level_dbov(&[]), -127);
        assert_eq!(audio_level_dbov(&[0i16; 960]), -127);
        let full: Vec<i16> = vec![i16::MAX; 960];
        assert_eq!(audio_level_dbov(&full), 0);
        // 1 kHz sine at ~0.3 amplitude ≈ -13 dBov (matches upstream's test).
        let sine: Vec<i16> = (0..960)
            .map(|i| {
                let t = i as f32 / 48_000.0;
                (0.3 * (2.0 * std::f32::consts::PI * 1000.0 * t).sin() * 32767.0) as i16
            })
            .collect();
        let db = audio_level_dbov(&sine);
        assert!((-20..=-8).contains(&db), "got {db} dBov");
    }

    fn make_encoder() -> EncoderInner {
        EncoderInner::new(32_000, true).expect("encoder")
    }

    fn sine_pcm(samples: usize) -> Vec<u8> {
        (0..samples)
            .flat_map(|i| {
                let t = i as f32 / 48_000.0;
                let s = (0.3 * (2.0 * std::f32::consts::PI * 440.0 * t).sin() * 32767.0) as i16;
                s.to_le_bytes()
            })
            .collect()
    }

    #[test]
    fn encoder_buffers_partials_across_calls() {
        let mut enc = make_encoder();
        // 1.5 frames, then the missing half: 1 frame out, then 1 more.
        let pcm = sine_pcm(FRAME_SAMPLES + FRAME_SAMPLES / 2);
        assert_eq!(enc.drain(&pcm).unwrap().len(), 1);
        let pcm = sine_pcm(FRAME_SAMPLES / 2);
        assert_eq!(enc.drain(&pcm).unwrap().len(), 1);
        assert!(enc.carry.is_empty());
    }

    #[test]
    fn encoder_seq_and_ts_advance_per_frame() {
        let mut enc = make_encoder();
        let frames = enc.drain(&sine_pcm(3 * FRAME_SAMPLES)).unwrap();
        assert_eq!(frames.len(), 3);
        for (i, frame) in frames.iter().enumerate() {
            let seq = u16::from_be_bytes([frame[0], frame[1]]);
            let ts = u32::from_be_bytes([frame[2], frame[3], frame[4], frame[5]]);
            assert_eq!(seq, i as u16);
            assert_eq!(ts, (i as u32) * FRAME_SAMPLES as u32);
        }
    }

    #[test]
    fn encoder_rejects_odd_byte_input() {
        let mut enc = make_encoder();
        assert!(enc.drain(&[0u8; 3]).is_err());
    }

    #[test]
    fn round_trip_through_decoder() {
        let mut enc = make_encoder();
        let frames = enc.drain(&sine_pcm(2 * FRAME_SAMPLES)).unwrap();
        assert_eq!(frames.len(), 2);
        let mut dec = DecoderInner {
            decoders: HashMap::new(),
            scratch: vec![0i16; MAX_DECODE_SAMPLES],
        };
        for frame in frames {
            // Simulate the relay's 1-byte peer_index prefix.
            let mut wire = vec![3u8];
            wire.extend_from_slice(&frame);
            let (idx, _, _, level, flags, n) = dec.decode(&wire).expect("decode");
            assert_eq!(idx, 3);
            assert_eq!(flags & FLAG_DTX, 0, "speech frame must not be DTX-flagged");
            assert!((-127..=0).contains(&level));
            assert_eq!(n, FRAME_SAMPLES);
        }
        assert_eq!(dec.decoders.len(), 1, "one stateful decoder per peer");
    }

    #[test]
    fn silence_with_dtx_produces_tiny_flagged_frames() {
        let mut enc = make_encoder();
        // Feed 1 s of silence; after Opus's DTX hangover (~400 ms of normal
        // frames), packets shrink to comfort noise and get the DTX flag.
        let silence = vec![0u8; 50 * FRAME_SAMPLES * 2];
        let frames = enc.drain(&silence).unwrap();
        assert!(!frames.is_empty());
        let dtx_frames: Vec<_> = frames.iter().filter(|f| f[7] & FLAG_DTX != 0).collect();
        assert!(
            !dtx_frames.is_empty(),
            "sustained silence must produce DTX-flagged frames"
        );
        for f in dtx_frames {
            assert!(f.len() <= V2_HEADER_LEN + DTX_MAX_PAYLOAD);
            let level = f[6] as i8;
            assert_eq!(level, -127, "silence level is the floor");
        }
    }
}
