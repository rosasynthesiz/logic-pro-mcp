"""What the dialogue detector must not regress.

Two of these tests exist because of specific past failures, and they are the reason the file is worth
having:

* the music gate must never swallow real speech — an earlier modulation-based gate that "obviously" made
  sense cut a real 132-cue production run down to 11 before anyone measured it;
* cues must stay short enough to be useful as markers — the shipped 2026-09-08 set contained a 32.3 s cue.

The synthetic signals come from conftest: `speechlike` carries formant-band energy gated at a syllabic
~4 Hz with a moving spectrum, `musiclike` is a sustained chord. They are not speech and not music; they
are the smallest signals that carry the properties the detector actually keys on.
"""
from __future__ import annotations

import numpy as np
import pytest

from logic_mcp import dialogue

from .conftest import musiclike, silence, speechlike


# ---------------------------------------------------------------------------
# detection basics
# ---------------------------------------------------------------------------

def test_finds_speechlike_audio(speech_wav):
    res = dialogue.detect_cues(speech_wav)
    assert res["count"] > 0, "the speech-like signal must produce at least one cue"
    assert res["speech_seconds"] > 0
    assert all(s["end"] > s["start"] for s in res["segments"])


def test_segments_are_ordered_and_disjoint(speech_wav):
    segs = dialogue.detect_cues(speech_wav)["segments"]
    for a, b in zip(segs, segs[1:]):
        assert a["end"] <= b["start"] + 1e-6, "cues must not overlap"


def test_silence_produces_nothing(write_wav):
    path = write_wav("quiet.wav", silence(4.0))
    res = dialogue.detect_cues(path)
    assert res["count"] == 0
    assert res["speech_seconds"] == 0.0


def test_result_shape_is_stable(speech_wav):
    res = dialogue.detect_cues(speech_wav)
    for key in ("segments", "count", "rejected", "speech_seconds", "speech_ratio", "seconds", "params"):
        assert key in res, f"callers (server.py, cuewav export) rely on {key!r}"
    assert res["count"] == len(res["segments"])


def test_rejects_bad_arguments(speech_wav):
    with pytest.raises(ValueError):
        dialogue.detect_cues(speech_wav, sensitivity=1.5)
    with pytest.raises(ValueError):
        dialogue.detect_cues(speech_wav, music_gate="nonsense")


# ---------------------------------------------------------------------------
# over-merging: cues have to stay short enough to be useful as markers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("max_duration", [2.0, 4.0])
def test_max_duration_is_enforced(write_wav, max_duration):
    """One long continuous stretch must be split, not emitted as a single 20 s cue."""
    path = write_wav("long.wav", np.concatenate([silence(0.5), speechlike(20.0), silence(0.5)]))
    res = dialogue.detect_cues(path, max_duration=max_duration, music_gate="off")
    assert res["count"] > 0
    longest = max(s["duration"] for s in res["segments"])
    # a split lands on a frame boundary, so allow one hop of slack rather than demanding exactness
    assert longest <= max_duration + 0.05, f"cue of {longest:.2f}s exceeds max_duration={max_duration}"


def test_splitting_preserves_coverage(write_wav):
    """Splitting must cut cues, not drop audio: total speech time survives the split."""
    path = write_wav("long.wav", np.concatenate([silence(0.5), speechlike(20.0), silence(0.5)]))
    whole = dialogue.detect_cues(path, max_duration=0, music_gate="off")
    split = dialogue.detect_cues(path, max_duration=3.0, music_gate="off")
    assert split["count"] > whole["count"], "a 20 s stretch capped at 3 s must yield more cues"
    assert split["speech_seconds"] == pytest.approx(whole["speech_seconds"], rel=0.05)


def test_split_never_creates_sub_minimum_cues(write_wav):
    path = write_wav("long.wav", np.concatenate([silence(0.5), speechlike(20.0), silence(0.5)]))
    res = dialogue.detect_cues(path, max_duration=1.0, min_speech=0.35, music_gate="off")
    assert all(s["duration"] >= 0.35 - 1e-6 for s in res["segments"])


def test_max_duration_zero_disables_splitting(write_wav):
    path = write_wav("long.wav", np.concatenate([silence(0.5), speechlike(20.0), silence(0.5)]))
    a = dialogue.detect_cues(path, max_duration=0, music_gate="off")["count"]
    b = dialogue.detect_cues(path, max_duration=3.0, music_gate="off")["count"]
    assert a < b


# ---------------------------------------------------------------------------
# the music gate — the half of this file that guards against the 132 -> 11 failure
# ---------------------------------------------------------------------------

def test_music_gate_does_not_swallow_speech(speech_wav):
    """THE regression test. Every gate level must keep finding the speech-like signal.

    If this fails, the gate is too aggressive — exactly the failure that destroyed a real production run.
    """
    off = dialogue.detect_cues(speech_wav, music_gate="off")["count"]
    assert off > 0
    for gate in ("safe", "strict"):
        got = dialogue.detect_cues(speech_wav, music_gate=gate)["count"]
        assert got > 0, f"music_gate={gate!r} rejected every cue in speech-like audio"
        assert got >= off * 0.5, f"music_gate={gate!r} dropped more than half the cues ({got} of {off})"


def test_music_gate_rejects_sustained_tone(write_wav):
    path = write_wav("chord.wav", np.concatenate([silence(0.5), musiclike(8.0), silence(0.5)]))
    loose = dialogue.detect_cues(path, music_gate="off")["count"]
    tight = dialogue.detect_cues(path, music_gate="safe")["count"]
    assert tight <= loose
    if loose:
        assert tight < loose, "a sustained chord should lose cues to the music gate"


def test_gate_off_is_the_permissive_baseline(write_wav):
    """off >= safe >= strict, always — the levels must be ordered, not merely different."""
    path = write_wav("mixed.wav", np.concatenate([speechlike(6.0), silence(1.0), musiclike(6.0)]))
    counts = [dialogue.detect_cues(path, music_gate=g)["count"] for g in ("off", "safe", "strict")]
    assert counts[0] >= counts[1] >= counts[2], f"gate levels not ordered: {counts}"


def test_rejected_segments_are_reported_not_silently_dropped(write_wav):
    """A wrong music call has to be auditable, so rejects carry their reason and their features."""
    path = write_wav("chord.wav", np.concatenate([silence(0.5), musiclike(8.0), silence(0.5)]))
    res = dialogue.detect_cues(path, music_gate="safe")
    if res["rejected"]:
        r = res["rejected"][0]
        assert r["rejected_as"] in {"flux_var", "f0_hold"}
        assert "music" in r and "flux_var" in r["music"]
        assert r["end"] > r["start"]


def test_kept_segments_carry_their_music_features(speech_wav):
    res = dialogue.detect_cues(speech_wav, music_gate="safe")
    scored = [s for s in res["segments"] if "music" in s]
    assert scored, "segments long enough to score should expose their music features"
    assert all(np.isfinite(s["music"]["flux_var"]) for s in scored)


def test_gate_off_scores_nothing(speech_wav):
    res = dialogue.detect_cues(speech_wav, music_gate="off")
    assert res["rejected"] == []
    assert all("music" not in s for s in res["segments"])


def test_flux_var_separates_the_two_signals(write_wav):
    """The measured claim the default gate rests on: speech-like churn > sustained-tone churn.

    Measured on real material (2026-09-09): dialogue median 0.0071, instrumental median 0.0029.
    """
    sp = dialogue.detect_cues(write_wav("s.wav", speechlike(8.0)), music_gate="off")
    mu = dialogue.detect_cues(write_wav("m.wav", musiclike(8.0)), music_gate="off")

    def flux(res, path):
        x, sr = dialogue._load_mono_16k(path, None)
        feats = dialogue._frame_features(x, sr, 25.0, 10.0, dialogue.SPEECH_BAND_HZ, dialogue.MOD_BAND_HZ)
        vals = [dialogue._music_features(s, feats, False).get("flux_var") for s in res["segments"]]
        return [v for v in vals if v is not None]

    sf_ = flux(sp, write_wav("s.wav", speechlike(8.0)))
    mf_ = flux(mu, write_wav("m.wav", musiclike(8.0)))
    if sf_ and mf_:
        assert float(np.median(sf_)) > float(np.median(mf_))


# ---------------------------------------------------------------------------
# granularity re-merging
# ---------------------------------------------------------------------------

def test_granularity_merges_monotonically():
    segs = [{"start": 0.0, "end": 1.0, "duration": 1.0, "confidence": 0.8},
            {"start": 1.5, "end": 2.0, "duration": 0.5, "confidence": 0.6},
            {"start": 12.0, "end": 13.0, "duration": 1.0, "confidence": 0.7}]
    line = dialogue.to_granularity(segs, "line")
    burst = dialogue.to_granularity(segs, "burst")
    scene = dialogue.to_granularity(segs, "scene")
    assert len(line) == 3
    assert len(burst) == 2, "a 0.5 s gap is inside the 1.2 s burst window"
    assert len(scene) == 2, "the 10 s gap exceeds the 8 s scene window"
    assert scene[0]["end"] == 2.0


def test_granularity_rejects_unknown_mode():
    with pytest.raises(ValueError):
        dialogue.to_granularity([], "epic")


def test_granularity_confidence_is_duration_weighted():
    segs = [{"start": 0.0, "end": 3.0, "duration": 3.0, "confidence": 1.0},
            {"start": 3.1, "end": 4.1, "duration": 1.0, "confidence": 0.0}]
    merged = dialogue.to_granularity(segs, "burst")
    assert len(merged) == 1
    assert merged[0]["confidence"] == pytest.approx(0.75, abs=0.01)


def test_granularity_does_not_mutate_input():
    segs = [{"start": 0.0, "end": 1.0, "duration": 1.0, "confidence": 0.5},
            {"start": 1.1, "end": 2.0, "duration": 0.9, "confidence": 0.5}]
    before = [dict(s) for s in segs]
    dialogue.to_granularity(segs, "scene")
    assert segs == before
