"""Dialogue-cue detection: where speech starts and stops inside a mixed programme track.

CORE DESIGN RULE (product requirement): no AI model, and language-independent — the same code has to
work on Tamil, Hindi, English, or anything else, because it never looks at phonemes or words. It only
looks at two acoustic properties that speech has and most music/FX does not, at the same time:

  1. energy concentration in the speech formant band (~300 Hz - 3.4 kHz — the classic telephone band,
     chosen because it is where vowel formants and most consonant energy live regardless of language)
  2. syllabic amplitude modulation (~3-8 Hz — the rate at which speech energy rises and falls as syllables
     are produced, again a property of the vocal tract and speech rate in general, not of any language)

A sustained pad or drone can sit in the speech band without modulating at 3-8 Hz. A rhythmic music bed can
modulate without concentrating in the speech band. Real dialogue tends to do both together, so the two
features are combined (as percentile *ranks* within the file, not raw units) rather than either alone.

Everything is derived from the file's own statistics (percentiles), never a hardcoded dB or ratio, so it
self-calibrates to any programme level or genre. See dialogue.detect_cues.__doc__ for the pipeline.

Resampling to the fixed analysis rate and the syllabic-rate bandpass are both done with plain FFT masking
(rfft -> zero unwanted bins -> irfft) — the same trick audio.true_peak_db already uses for oversampling —
so this module needs nothing beyond numpy + soundfile, no scipy dependency.
"""
from __future__ import annotations

import math

import numpy as np
import soundfile as sf
from numpy.lib.stride_tricks import sliding_window_view

ANALYSIS_SR = 16000                  # fixed working rate: plenty for 300 Hz-3.4 kHz + 3-8 Hz features
SPEECH_BAND_HZ = (300.0, 3400.0)     # formant / telephone band — language-independent by construction
MOD_BAND_HZ = (3.0, 8.0)             # syllabic rate for typical human speech, any language
SPEECH_RATIO_FLOOR = 0.65   # absolute formant-band concentration a frame must reach to count as speech
GRANULARITY_GAPS = {"line": 0.0, "burst": 1.2, "scene": 8.0}

# --- music gate -------------------------------------------------------------------------------------
# Measured 2026-09-09 over 119 detected segments of a 137-track Tamil dialogue programme
# and 40 segments from three instrumental-only files (cinematic score, solo cello, acoustic guitar):
#
#   feature            dialogue median   music median   separates?
#   low_energy_ratio        0.233           0.237       NO  — the textbook speech/music feature, useless here
#   zcr_std                 0.063           0.052       weak, overlapping
#   flux_var                0.0071          0.0029      YES — 2.4x, the only clean single discriminator
#   f0_hold                 0.585      0.297 / 1.000    two-sided only (percussion low, held notes high)
#
# So the default gate uses flux_var ALONE: at >= 0.0021 it keeps 95 % of dialogue segments and removes
# 42 % of the music ones. Adding an f0_hold cap on top removed a further 5 % of dialogue and NO extra
# music, so it is confined to gate="strict" (84.9 % dialogue kept, 67.5 % of music removed).
#
# Bias is deliberately toward recall: a spurious marker is deleted in one click, a missing one means a
# missed line. Thresholds come from four files — thin evidence. Re-measure before tightening them, and
# note the history: an earlier modulation-based gate that "obviously" made sense cut a real 132-cue run
# down to 11. Every segment carries its own `music` features in the output so a bad call is auditable.
FLUX_VAR_MIN = 0.0021
F0_BAND_HZ = (70.0, 400.0)
F0_HOLD_RANGE = (0.28, 0.93)
VOICED_HARMONICITY = 0.30
MUSIC_GATES = ("off", "safe", "strict")
MAX_CUE_SECONDS = 12.0      # longer than this is useless as a marker; split at the quietest interior frame


# ---------------------------------------------------------------------------
# loading / low-level DSP helpers
# ---------------------------------------------------------------------------

def _resample_fft(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    """Whole-signal Fourier resample: rfft, truncate or zero-pad the spectrum, irfft back.

    Exact for a band-limited signal and needs no extra library. Ringing at the two file edges is
    irrelevant here — nothing downstream cares about sample-accurate edges, only 10 ms-scale features.
    """
    if sr_from == sr_to or len(x) == 0:
        return x.astype(np.float32)
    n = len(x)
    n_to = int(round(n * sr_to / sr_from))
    spec = np.fft.rfft(x)
    m_from, m_to = len(spec), n_to // 2 + 1
    if m_to <= m_from:
        spec2 = spec[:m_to]
    else:
        spec2 = np.zeros(m_to, dtype=spec.dtype)
        spec2[:m_from] = spec
    y = np.fft.irfft(spec2, n=n_to) * (n_to / n)
    return y.astype(np.float32)


def _load_mono_16k(path: str, max_seconds: float | None) -> tuple[np.ndarray, int]:
    """Decode with soundfile (as audio.py does), fold to mono, resample to ANALYSIS_SR."""
    sr_actual = sf.info(path).samplerate
    frames = int(max_seconds * sr_actual) if max_seconds else -1
    data, sr = sf.read(path, frames=frames, dtype="float32", always_2d=True)
    mono = data.mean(axis=1)
    if sr != ANALYSIS_SR:
        mono = _resample_fft(mono, sr, ANALYSIS_SR)
    return mono, ANALYSIS_SR


def _bandpass_fft(x: np.ndarray, fs: float, lo: float, hi: float) -> np.ndarray:
    """Zero-phase bandpass by masking the FFT — used on the (slow, ~100 Hz) envelope signal, not audio."""
    n = len(x)
    if n < 4:
        return np.zeros_like(x)
    freqs = np.fft.rfftfreq(n, d=1.0 / fs)
    spec = np.fft.rfft(x)
    spec[~((freqs >= lo) & (freqs <= hi))] = 0.0
    return np.fft.irfft(spec, n=n)


def _rank(x: np.ndarray) -> np.ndarray:
    """Each value's fractional rank (0..1) within the array — an adaptive, unit-free normalisation."""
    n = len(x)
    if n < 2:
        return np.zeros_like(x, dtype=np.float64)
    order = np.argsort(x, kind="mergesort")
    ranks = np.empty(n, dtype=np.float64)
    ranks[order] = np.arange(n)
    return ranks / (n - 1)


# ---------------------------------------------------------------------------
# per-frame feature extraction
# ---------------------------------------------------------------------------

def _frame_features(x: np.ndarray, sr: int, frame_ms: float, hop_ms: float,
                     speech_band: tuple[float, float], mod_band: tuple[float, float]) -> dict:
    n = int(round(sr * frame_ms / 1000))
    hop = int(round(sr * hop_ms / 1000))
    if len(x) < n or n < 8 or hop < 1:
        return {}
    framed = sliding_window_view(x, n)[::hop].astype(np.float32)   # (num_frames, n), zero-copy view then strided
    if len(framed) < 8:
        return {}                                                  # too short to say anything about rhythm
    window = np.hanning(n).astype(np.float32)
    spec = np.fft.rfft(framed * window, axis=1)
    mag2 = spec.real.astype(np.float64) ** 2 + spec.imag.astype(np.float64) ** 2
    freqs = np.fft.rfftfreq(n, d=1.0 / sr)

    lo, hi = speech_band
    band_mask = (freqs >= lo) & (freqs <= hi)
    total_energy = mag2.sum(axis=1) + 1e-12
    speech_energy = mag2[:, band_mask].sum(axis=1)
    speech_ratio = speech_energy / total_energy                    # feature 1: formant-band concentration

    frame_rms = np.sqrt((framed.astype(np.float64) ** 2).mean(axis=1))
    frame_db = 20 * np.log10(frame_rms + 1e-9)

    hop_s = hop / sr
    fs_env = 1.0 / hop_s
    env = np.sqrt(speech_energy + 1e-12)                            # envelope of the speech-band energy itself
    log_env = np.log(env + 1e-9)
    mlo, mhi = mod_band
    mod = _bandpass_fft(log_env, fs_env, mlo, min(mhi, fs_env / 2 - 0.01))
    # ~300 ms local window (a few syllables), but never longer than the signal itself: convolving with a
    # kernel longer than the input raises a broadcast error on very short files.
    win_frames = max(3, min(int(round(0.30 / hop_s)), len(log_env)))
    mod_energy = np.convolve(mod ** 2, np.ones(win_frames) / win_frames, mode="same")  # feature 2: 3-8 Hz modulation power

    # Spectral flux, on the L1-normalised magnitude spectrum so it measures how fast the spectral SHAPE
    # changes, independent of level. Only the positive part is summed (the classic onset-flux definition):
    # what matters is energy appearing in new bins, not decaying out of old ones. Used by the music gate.
    mag = np.sqrt(mag2)
    mag /= mag.sum(axis=1, keepdims=True) + 1e-12
    flux = np.empty(len(mag), dtype=np.float64)
    flux[0] = 0.0
    flux[1:] = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)

    # `framed` is deliberately NOT returned: it is a full float32 copy of the signal (~96 MB for a
    # 10-minute file) and the only thing that wants time-domain samples is the strict gate's pitch
    # tracker, which reframes the few seconds it actually needs.
    return dict(speech_ratio=speech_ratio, mod_energy=mod_energy, frame_db=frame_db, flux=flux,
                hop_s=hop_s, n=n, sr=sr)


def _segments_from_score(score: np.ndarray, hop_s: float, enter_thr: float, exit_thr: float,
                          min_speech: float, max_gap: float, eligible: np.ndarray) -> list[dict]:
    """Schmitt-trigger hysteresis over the frame score: high bar to start a cue, lower bar to keep it alive,
    so a momentary dip mid-sentence does not chop one line into three."""
    segments: list[dict] = []
    max_gap_frames = max(1, int(round(max_gap / hop_s)))
    state = False
    start_i = last_active_i = 0
    gap = 0

    def close(end_i: int):
        start, end = start_i * hop_s, end_i * hop_s + hop_s
        dur = end - start
        if dur >= min_speech:
            segments.append({"start": round(start, 3), "end": round(end, 3), "duration": round(dur, 3),
                              "confidence": round(float(score[start_i:end_i + 1].mean()), 3)})

    for i, s in enumerate(score):
        active = bool(eligible[i]) and s >= (exit_thr if state else enter_thr)
        if not state:
            if active:
                state, start_i, last_active_i, gap = True, i, i, 0
        else:
            if active:
                last_active_i, gap = i, 0
            else:
                gap += 1
                if gap > max_gap_frames:
                    close(last_active_i)
                    state = False
    if state:
        close(last_active_i)
    return segments


def _split_long(segments: list[dict], score: np.ndarray, hop_s: float, max_duration: float,
                 min_speech: float) -> list[dict]:
    """Break segments longer than `max_duration` at their least speech-like interior frame.

    Hysteresis deliberately bridges pauses so one sentence does not become three, but on continuous
    back-and-forth dialogue it runs cues together — the shipped 2026-09-08 set had a 32.3 s cue and 8 over
    10 s, which is useless as a marker. Splitting at the score MINIMUM (rather than at a fixed interval)
    puts the cut in the natural pause the trigger bridged over, so both halves stay whole utterances.

    The split point is kept at least `min_speech` from either end so a split can never manufacture a
    segment shorter than the caller's own floor; a segment with no legal interior split is left long
    rather than chopped arbitrarily.
    """
    if max_duration <= 0:
        return segments
    guard = max(1, int(round(min_speech / hop_s)))
    out: list[dict] = []
    queue = sorted((dict(s) for s in segments), key=lambda s: s["start"])
    while queue:
        seg = queue.pop(0)
        if seg["duration"] <= max_duration:
            out.append(seg)
            continue
        a, b = int(round(seg["start"] / hop_s)), int(round(seg["end"] / hop_s))
        lo, hi = a + guard, min(b - guard, len(score) - 1)
        if hi <= lo:
            out.append(seg)                      # nowhere legal to cut — keep it whole and honest
            continue
        cut = lo + int(np.argmin(score[lo:hi]))
        left = {"start": seg["start"], "end": round(cut * hop_s, 3)}
        right = {"start": round(cut * hop_s, 3), "end": seg["end"]}
        for half in (left, right):
            half["duration"] = round(half["end"] - half["start"], 3)
            ha, hb = int(round(half["start"] / hop_s)), int(round(half["end"] / hop_s))
            hb = min(max(hb, ha + 1), len(score))
            half["confidence"] = round(float(score[ha:hb].mean()), 3)
        queue = [left, right] + queue
    return sorted(out, key=lambda s: s["start"])


# ---------------------------------------------------------------------------
# music rejection
# ---------------------------------------------------------------------------

def _f0_hold(x: np.ndarray, sr: int, frame_ms: float = 25.0, hop_ms: float = 10.0) -> float:
    """Fraction of consecutive voiced frames holding the same pitch to within 2 %.

    Autocorrelation f0 over 70-400 Hz. A held instrumental note scores ~1.0; speech glides constantly and
    sits near 0.6; percussion has almost no stable pitch at all and sits near 0.3.

    Takes raw samples and frames them here, so only the seconds belonging to one candidate segment are
    ever in memory rather than a framed copy of the whole file.
    """
    n = int(round(sr * frame_ms / 1000))
    hop = int(round(sr * hop_ms / 1000))
    if len(x) < n or n < 16 or hop < 1:
        return 0.0
    framed = sliding_window_view(x, n)[::hop].astype(np.float32)
    if len(framed) < 4:
        return 0.0
    w = np.hanning(n).astype(np.float32)
    nfft = 1 << (2 * n - 1).bit_length()
    spec = np.fft.rfft(framed * w, n=nfft, axis=1)
    ac = np.fft.irfft(spec * np.conj(spec), n=nfft, axis=1)[:, :n]
    ac = ac / (ac[:, :1] + 1e-12)
    lo_lag, hi_lag = int(sr / F0_BAND_HZ[1]), min(int(sr / F0_BAND_HZ[0]), n - 1)
    if hi_lag <= lo_lag:
        return 0.0
    band = ac[:, lo_lag:hi_lag]
    harm = band.max(axis=1)
    f0 = sr / (band.argmax(axis=1) + lo_lag)
    voiced = f0[harm > VOICED_HARMONICITY]
    if len(voiced) < 2:
        return 0.0
    return float(np.mean(np.abs(np.diff(voiced)) / voiced[:-1] < 0.02))


def _music_features(seg: dict, feats: dict, need_f0: bool, x: np.ndarray | None = None) -> dict:
    hop_s = feats["hop_s"]
    a = int(round(seg["start"] / hop_s))
    b = min(int(round(seg["end"] / hop_s)), len(feats["flux"]))
    if b - a < 8:
        return {}                                # too short to characterise; the gate lets it through
    out = {"flux_var": round(float(np.var(feats["flux"][a:b])), 6)}
    if need_f0 and x is not None:
        sr = feats["sr"]
        sa, sb = int(seg["start"] * sr), min(int(seg["end"] * sr), len(x))
        out["f0_hold"] = round(_f0_hold(x[sa:sb], sr), 3)
    return out


def _music_verdict(mf: dict, gate: str) -> str | None:
    """Return the reason this segment looks like music, or None to keep it."""
    if not mf:
        return None
    if mf.get("flux_var", 1.0) < FLUX_VAR_MIN:
        return "flux_var"                        # spectral shape too steady — sustained/looped music
    if gate == "strict":
        hold = mf.get("f0_hold")
        if hold is not None and not (F0_HOLD_RANGE[0] <= hold <= F0_HOLD_RANGE[1]):
            return "f0_hold"                     # held notes (tonal bed) or no stable pitch at all (percussion)
    return None


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------

def detect_cues(path: str, *, min_speech: float = 0.35, max_gap: float = 0.6, sensitivity: float = 0.5,
                 max_seconds: float | None = None, frame_ms: float = 25.0, hop_ms: float = 10.0,
                 max_duration: float = MAX_CUE_SECONDS, music_gate: str = "safe",
                 speech_band: tuple[float, float] = SPEECH_BAND_HZ,
                 mod_band: tuple[float, float] = MOD_BAND_HZ) -> dict:
    """Find dialogue segments in a mixed track.

    Pipeline: decode -> mono 16 kHz -> 25 ms/10 ms framed FFT -> two features per frame (speech-band energy
    ratio, 3-8 Hz modulation power of that band's envelope) -> each ranked to a 0..1 percentile within the
    file (the adaptive part: no fixed dB or ratio survives a level or genre change) -> weighted score ->
    hysteresis state machine (enter/exit thresholds from `sensitivity`, `min_speech` floor, `max_gap`
    bridging) -> segments.

    sensitivity in [0, 1]: higher finds more/shorter cues (lower bar to enter and to stay in a cue).
    max_duration: split any cue longer than this at its least speech-like interior frame (0 disables).
    music_gate: "off" | "safe" (spectral-flux variance) | "strict" (also two-sided pitch stability) —
        see the FLUX_VAR_MIN block above for the measurements behind the thresholds.
    """
    if not 0.0 <= sensitivity <= 1.0:
        raise ValueError("sensitivity must be within 0..1")
    if music_gate not in MUSIC_GATES:
        raise ValueError(f"music_gate must be one of {list(MUSIC_GATES)}")

    x, sr = _load_mono_16k(path, max_seconds)
    total_seconds = len(x) / sr
    feats = _frame_features(x, sr, frame_ms, hop_ms, speech_band, mod_band)
    params = {
        "min_speech": min_speech, "max_gap": max_gap, "sensitivity": sensitivity,
        "frame_ms": frame_ms, "hop_ms": hop_ms, "max_duration": max_duration, "music_gate": music_gate,
        "speech_band_hz": list(speech_band), "mod_band_hz": list(mod_band), "sample_rate": sr,
    }
    if not feats:
        return {"segments": [], "count": 0, "speech_seconds": 0.0, "speech_ratio": 0.0, "rejected": [],
                "seconds": round(total_seconds, 2), "params": params}

    r_speech = _rank(feats["speech_ratio"])
    r_mod = _rank(feats["mod_energy"])
    score = 0.55 * r_speech + 0.45 * r_mod                 # formant concentration weighted slightly above modulation

    frame_db = feats["frame_db"]
    floor_db = float(np.percentile(frame_db, 20))          # bottom quintile of frame loudness ~= noise floor
    eligible = frame_db > (floor_db + 3.0)                 # never call near-silence "speech" no matter the score

    # Ranks are relative to THIS file, so a file containing no speech at all still ranks its loudest
    # material top and reports it as dialogue. Guard with an ABSOLUTE floor on formant-band
    # concentration. Measured on this production material: speech frames sit at ~0.84 median
    # speech-band ratio, music at ~0.09 -- roughly 10x apart, whereas 3-8 Hz modulation energy overlaps
    # (0.40 vs 0.28 median) and does NOT separate them.
    eligible &= feats["speech_ratio"] > SPEECH_RATIO_FLOOR

    enter_frac = min(0.9, 0.15 + 0.45 * sensitivity)        # fraction of frames allowed to *start* a cue
    exit_frac = min(0.95, enter_frac + 0.20)                # a larger, looser fraction keeps a cue alive
    enter_thr = float(np.percentile(score, 100 * (1 - enter_frac)))
    exit_thr = float(np.percentile(score, 100 * (1 - exit_frac)))
    if exit_thr >= enter_thr:
        exit_thr = enter_thr - 1e-6

    segments = _segments_from_score(score, feats["hop_s"], enter_thr, exit_thr, min_speech, max_gap, eligible)
    segments = _split_long(segments, score, feats["hop_s"], max_duration, min_speech)

    kept, rejected = [], []
    for seg in segments:
        mf = _music_features(seg, feats, need_f0=(music_gate == "strict"), x=x) if music_gate != "off" else {}
        if mf:
            seg["music"] = mf
        reason = _music_verdict(mf, music_gate) if music_gate != "off" else None
        if reason:
            rejected.append({**seg, "rejected_as": reason})
        else:
            kept.append(seg)

    speech_seconds = round(sum(s["duration"] for s in kept), 2)
    params.update(enter_threshold=round(enter_thr, 4), exit_threshold=round(exit_thr, 4),
                  noise_floor_db=round(floor_db, 1))

    return {
        "segments": kept,
        "count": len(kept),
        "rejected": rejected,                    # kept in the payload so a wrong music call is auditable
        "speech_seconds": speech_seconds,
        "speech_ratio": round(speech_seconds / total_seconds, 4) if total_seconds > 0 else 0.0,
        "seconds": round(total_seconds, 2),
        "params": params,
    }


def to_granularity(segments: list[dict], mode: str = "line") -> list[dict]:
    """Re-merge detect_cues() segments to a coarser granularity.

    "line"  — the raw cues as detected (one per utterance/phrase, gaps up to max_gap already bridged).
    "burst" — quick back-and-forth exchanges glued together (gaps up to 1.2 s absorbed) — one marker per
              conversational volley rather than per line.
    "scene" — long dialogue passages, tolerating pauses up to 8 s (a beat, a reaction, a short FX sting)
              before calling it a new scene of dialogue.
    """
    if mode not in GRANULARITY_GAPS:
        raise ValueError(f"mode must be one of {sorted(GRANULARITY_GAPS)}")
    gap = GRANULARITY_GAPS[mode]
    segs = [dict(s) for s in sorted(segments, key=lambda s: s["start"])]
    if gap <= 0 or len(segs) < 2:
        return segs

    merged = [segs[0]]
    for s in segs[1:]:
        last = merged[-1]
        if s["start"] - last["end"] <= gap:
            w0, w1 = last["duration"], s["duration"]
            last["end"] = max(last["end"], s["end"])
            last["duration"] = round(last["end"] - last["start"], 3)
            total_w = w0 + w1
            if total_w > 0:
                last["confidence"] = round((last["confidence"] * w0 + s["confidence"] * w1) / total_w, 3)
        else:
            merged.append(s)
    return merged
