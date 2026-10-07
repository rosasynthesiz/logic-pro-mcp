"""Pins down logic_mcp.audio: the measurements the leveling loop bounces-and-measures instead of trusting
Logic's meters (which report clipping only, no numbers).

Each test checks one physically-known property of a synthetic signal rather than a fixed magic number,
so the tests stay meaningful if a coefficient gets retuned for a real bug fix.
"""
from __future__ import annotations

import math

import numpy as np
import pytest

from logic_mcp import audio

from .conftest import SR, silence, sine


def _stereo(x: np.ndarray) -> np.ndarray:
    """audio.py's functions all expect (n_samples, n_channels) — conftest's mono helpers are 1D."""
    return np.column_stack([x, x])


# ---------------------------------------------------------------------------
# integrated_lufs
# ---------------------------------------------------------------------------

def test_1khz_sine_at_minus_20_dbfs_lands_near_minus_20_lufs():
    # K-weighting is a high-shelf above ~1.5 kHz plus a high-pass at ~38 Hz -- at 1 kHz, comfortably
    # below the shelf and well above the high-pass, the filter is close to 0 dB. So a -20 dBFS tone
    # should read close to -20 LUFS. The +/-1.5 LU tolerance is loose because "close to flat" is not
    # "exactly flat": there is still some shelf roll-on approaching 1.5 kHz, plus the -0.691 LUFS offset
    # and block-gating math, all of which nudge the number around without changing the ballpark.
    x = _stereo(sine(1000.0, 2.0, SR, amp=0.1))            # 0.1 amplitude == -20 dBFS
    lufs = audio.integrated_lufs(x, SR)
    assert lufs is not None
    assert abs(lufs - (-20.0)) < 1.5


def test_loudness_is_amplitude_ordered():
    quiet = _stereo(sine(1000.0, 2.0, SR, amp=0.03))
    loud = _stereo(sine(1000.0, 2.0, SR, amp=0.3))
    lufs_quiet = audio.integrated_lufs(quiet, SR)
    lufs_loud = audio.integrated_lufs(loud, SR)
    assert lufs_quiet is not None and lufs_loud is not None
    assert lufs_loud > lufs_quiet


def test_integrated_lufs_returns_none_for_silence_not_a_crash():
    x = _stereo(silence(2.0, SR))
    assert audio.integrated_lufs(x, SR) is None


def test_integrated_lufs_returns_none_when_too_short_for_one_block():
    # BS.1770 measures in 400 ms blocks -- less than one block's worth of audio has nothing to gate.
    x = _stereo(sine(1000.0, 0.05, SR, amp=0.5))
    assert audio.integrated_lufs(x, SR) is None


# ---------------------------------------------------------------------------
# true_peak_db
# ---------------------------------------------------------------------------

def test_full_scale_sine_true_peak_near_0_dbtp():
    x = _stereo(sine(1000.0, 1.0, SR, amp=1.0))
    tp = audio.true_peak_db(x)
    assert abs(tp - 0.0) < 0.5


def test_inter_sample_peak_measures_above_sample_peak():
    # An incommensurate frequency almost never samples exactly at the waveform's true peak, so the raw
    # sample-domain max undershoots the sine's real amplitude. true_peak_db reconstructs the
    # inter-sample waveform (4x oversampling) and must recover a peak at or above what a naive
    # np.abs(x).max() sees -- that gap is the entire reason true-peak metering exists.
    x = _stereo(sine(997.3, 0.5, SR, amp=0.999))
    sample_peak_db = audio.db(float(np.abs(x).max()))
    tp = audio.true_peak_db(x)
    assert tp > sample_peak_db


def test_true_peak_of_silence_is_sample_peak_not_a_crash():
    x = _stereo(silence(1.0, SR))
    assert audio.true_peak_db(x) == audio.db(0.0)
    assert math.isinf(audio.true_peak_db(x))            # documented behaviour: db(0.0) == -inf


# ---------------------------------------------------------------------------
# db()
# ---------------------------------------------------------------------------

def test_db_of_full_scale_is_zero():
    assert audio.db(1.0) == pytest.approx(0.0, abs=1e-9)


def test_db_of_zero_is_negative_infinity():
    assert audio.db(0.0) == -float("inf")


# ---------------------------------------------------------------------------
# crest factor (via Measurement.measure)
# ---------------------------------------------------------------------------

def test_sine_crest_factor_is_about_3db(write_wav):
    path = write_wav("sine.wav", sine(1000.0, 2.0, SR, amp=0.5))
    m = audio.measure(path)
    assert m.crest_db is not None
    # peak/RMS of a pure sine is sqrt(2) => 20*log10(sqrt(2)) ~= 3.01 dB
    assert abs(m.crest_db - 3.01) < 0.5


def test_white_noise_crest_factor_exceeds_sine():
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(SR * 2) * 0.2).astype(np.float32)
    noise = np.clip(noise, -1.0, 1.0)
    x = _stereo(noise)
    peak = float(np.abs(x).max())
    rms = float(np.sqrt((x.astype(np.float64) ** 2).mean()))
    noise_crest = audio.db(peak) - audio.db(rms)

    sine_x = _stereo(sine(1000.0, 2.0, SR, amp=0.2))
    sine_peak = float(np.abs(sine_x).max())
    sine_rms = float(np.sqrt((sine_x.astype(np.float64) ** 2).mean()))
    sine_crest = audio.db(sine_peak) - audio.db(sine_rms)

    # Gaussian noise has a much less predictable, spikier waveform than a sine of the same RMS, so its
    # peak sits proportionally further above its RMS -- crest factor is higher.
    assert noise_crest > sine_crest


# ---------------------------------------------------------------------------
# correlation
# ---------------------------------------------------------------------------

def test_correlation_identical_channels_is_plus_one():
    mono = sine(1000.0, 1.0, SR, amp=0.5)
    x = _stereo(mono)
    assert audio.correlation(x) == pytest.approx(1.0, abs=1e-6)


def test_correlation_inverted_channel_is_minus_one():
    mono = sine(1000.0, 1.0, SR, amp=0.5)
    x = np.column_stack([mono, -mono])
    assert audio.correlation(x) == pytest.approx(-1.0, abs=1e-6)


def test_correlation_uncorrelated_noise_is_near_zero():
    rng = np.random.default_rng(1)
    l = rng.standard_normal(SR * 4)
    r = rng.standard_normal(SR * 4)
    x = np.column_stack([l, r])
    corr = audio.correlation(x)
    assert corr is not None
    assert abs(corr) < 0.05                              # loose: independent Gaussians, large N


def test_correlation_is_none_for_mono():
    x = sine(1000.0, 1.0, SR, amp=0.5).reshape(-1, 1)
    assert audio.correlation(x) is None


# ---------------------------------------------------------------------------
# digital silence must never crash or leak NaN/inf out of the public Measurement
# ---------------------------------------------------------------------------

def _assert_all_finite_or_none(d: dict):
    for key, value in d.items():
        if isinstance(value, dict):
            _assert_all_finite_or_none(value)
        elif isinstance(value, float):
            assert math.isfinite(value), f"{key} = {value!r} is not finite"
        # None, str, int, bool: nothing to check


def test_measure_of_digital_silence_is_finite_everywhere(write_wav):
    path = write_wav("silence.wav", silence(2.0, SR))
    m = audio.measure(path)
    assert m.silent is True
    assert m.lufs is None                                 # silence never has a loudness reading
    # Measurement.to_dict() is the public surface -- it must have already scrubbed any inf/NaN raised
    # internally (db(0.0) is -inf) down to None, so nothing not-finite escapes to a caller.
    _assert_all_finite_or_none(m.to_dict())


def test_measure_of_near_silence_is_finite_everywhere(write_wav):
    # Not quite zero -- exercises the non-silent code paths (true_peak_db, band_energy, crest) at a
    # very low level, where log-domain math is most likely to misbehave.
    tiny = (sine(1000.0, 2.0, SR, amp=1e-4)).astype(np.float32)
    path = write_wav("near_silence.wav", tiny)
    m = audio.measure(path)
    _assert_all_finite_or_none(m.to_dict())
