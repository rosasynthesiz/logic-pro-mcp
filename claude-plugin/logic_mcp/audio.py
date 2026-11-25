"""Audio measurement for the leveling loop.

Logic's meters report clipping only — no numbers — so levels are measured from rendered audio instead.
That is what a human engineer does anyway: bounce, measure, decide, apply.

Loudness follows ITU-R BS.1770-4 / EBU R128: K-weighting, 400 ms blocks with 75% overlap, absolute gate at
-70 LUFS, then a relative gate 10 LU below the ungated mean.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, asdict

import numpy as np
import soundfile as sf

# Frequency bands used for spectral balance and for deciding what is fighting what.
BANDS = [("sub", 20, 60), ("low", 60, 120), ("low_mid", 120, 400), ("mid", 400, 2000),
         ("high_mid", 2000, 6000), ("high", 6000, 12000), ("air", 12000, 20000)]


def _biquad(b, a, x):
    """Direct-form II transposed along axis 0. scipy when present (orders of magnitude faster), else pure numpy."""
    try:
        from scipy.signal import lfilter
        return lfilter(np.asarray(b), np.asarray(a), x, axis=0)
    except ImportError:
        pass
    y = np.empty_like(x)
    z1 = np.zeros(x.shape[1:], dtype=np.float64)
    z2 = np.zeros(x.shape[1:], dtype=np.float64)
    b0, b1, b2 = b
    a1, a2 = a[1], a[2]
    for n in range(x.shape[0]):
        xn = x[n]
        yn = b0 * xn + z1
        z1 = b1 * xn - a1 * yn + z2
        z2 = b2 * xn - a2 * yn
        y[n] = yn
    return y


def _k_weight(x: np.ndarray, sr: int) -> np.ndarray:
    """BS.1770 pre-filter: a high-shelf then a high-pass, coefficients derived for the actual sample rate."""
    # stage 1 — high shelf (+4 dB above ~1.5 kHz)
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = math.tan(math.pi * f0 / sr)
    Vh = 10 ** (G / 20.0); Vb = Vh ** 0.4996667741545416
    den = 1 + K / Q + K * K
    b1_ = [(Vh + Vb * K / Q + K * K) / den, 2 * (K * K - Vh) / den, (Vh - Vb * K / Q + K * K) / den]
    a1_ = [1.0, 2 * (K * K - 1) / den, (1 - K / Q + K * K) / den]
    # stage 2 — high pass at ~38 Hz
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = math.tan(math.pi * f0 / sr)
    b2_ = [1.0, -2.0, 1.0]
    a2_ = [1.0, 2 * (K * K - 1) / (1 + K / Q + K * K), (1 - K / Q + K * K) / (1 + K / Q + K * K)]
    return _biquad(b2_, a2_, _biquad(b1_, a1_, x))


def _blocks(power: np.ndarray, sr: int, block: float = 0.400, overlap: float = 0.75):
    n = int(round(block * sr)); hop = int(round(n * (1 - overlap)))
    if power.shape[0] < n or hop < 1:
        return np.empty((0,))
    # cumulative sums turn the per-block means into two lookups instead of a Python loop
    summed = power.sum(axis=1)
    c = np.concatenate(([0.0], np.cumsum(summed)))
    starts = np.arange(0, power.shape[0] - n + 1, hop)
    return (c[starts + n] - c[starts]) / n


def integrated_lufs(x: np.ndarray, sr: int, channel_weights: np.ndarray | None = None) -> float | None:
    """Gated loudness in LUFS. None when the file is silent or too short for one 400 ms block."""
    y = _k_weight(x.astype(np.float64), sr)
    if channel_weights is None:
        channel_weights = np.ones(y.shape[1])
    mean_sq = _blocks(y ** 2 * channel_weights, sr)
    if mean_sq.size == 0:
        return None
    with np.errstate(divide="ignore"):
        l = -0.691 + 10 * np.log10(np.maximum(mean_sq, 1e-20))
    keep = l > -70.0                                   # absolute gate
    if not keep.any():
        return None
    rel = -0.691 + 10 * np.log10(mean_sq[keep].mean()) - 10.0   # relative gate
    keep &= l > rel
    if not keep.any():
        return None
    return float(-0.691 + 10 * np.log10(mean_sq[keep].mean()))


def true_peak_db(x: np.ndarray, oversample: int = 4, chunk: int = 1 << 18) -> float:
    """Peak after 4x upsampling, which catches inter-sample peaks a raw max misses.

    Runs over the WHOLE signal in overlapping chunks. Measuring only a prefix used to report a true peak
    below the sample peak, which is impossible and quietly wrecked the headroom guard downstream.
    """
    n = x.shape[0]
    if n < 8:
        return db(np.abs(x).max())
    overlap = 1024
    best = 0.0
    start = 0
    while start < n:
        seg = x[start:start + chunk + overlap]
        if seg.shape[0] < 8:
            break
        spec = np.fft.rfft(seg, axis=0)
        up = np.zeros((seg.shape[0] * oversample // 2 + 1, seg.shape[1]), dtype=spec.dtype)
        up[:spec.shape[0]] = spec
        y = np.fft.irfft(up, n=seg.shape[0] * oversample, axis=0) * oversample
        best = max(best, float(np.abs(y).max()))
        start += chunk
    return max(db(best), db(float(np.abs(x).max())))


def db(v: float) -> float:
    return float(20 * math.log10(v)) if v > 1e-12 else -float("inf")


def band_energy(x: np.ndarray, sr: int, seconds: float = 120.0) -> dict:
    """Share of total energy in each band, as a percentage — a spectral fingerprint.

    Averaged (Welch) over the signal at its FULL rate. An earlier version strided the samples to shorten
    the FFT but kept the original sample rate on the frequency axis, which relabelled every band by the
    stride factor — a bass stem came back with 0% low end.
    """
    mono = x.mean(axis=1)
    if len(mono) > int(sr * seconds):                  # a contiguous slice, never a stride
        mono = mono[:int(sr * seconds)]
    if len(mono) < 256:
        return {}
    nper = min(1 << 15, 1 << max(8, int(np.log2(len(mono) / 8))))
    try:
        from scipy.signal import welch
        freqs, psd = welch(mono, fs=sr, nperseg=nper, noverlap=nper // 2, scaling="spectrum")
    except ImportError:                                # manual Welch: averaged periodograms, same idea
        win = np.hanning(nper); hop = nper // 2
        segs = [mono[i:i + nper] for i in range(0, len(mono) - nper + 1, hop)] or [mono[:nper]]
        psd = np.mean([np.abs(np.fft.rfft(sg * win)) ** 2 for sg in segs], axis=0)
        freqs = np.fft.rfftfreq(nper, 1.0 / sr)
    total = psd.sum() or 1.0
    return {name: round(float(psd[(freqs >= lo) & (freqs < hi)].sum() / total * 100), 2) for name, lo, hi in BANDS}


def correlation(x: np.ndarray) -> float | None:
    """+1 mono, 0 wide, negative means phase trouble."""
    if x.shape[1] < 2:
        return None
    l, r = x[:, 0], x[:, 1]
    if l.std() < 1e-9 or r.std() < 1e-9:
        return None
    return float(np.clip(np.corrcoef(l, r)[0, 1], -1, 1))


@dataclass
class Measurement:
    path: str
    name: str
    seconds: float
    sample_rate: int
    channels: int
    lufs: float | None
    peak_db: float
    true_peak_db: float
    rms_db: float
    crest_db: float | None
    correlation: float | None
    bands: dict
    silent: bool

    def to_dict(self) -> dict:
        d = asdict(self)
        for k, v in d.items():
            if isinstance(v, float) and (math.isinf(v) or math.isnan(v)):
                d[k] = None
        return d


def measure(path: str, max_seconds: float = 600.0) -> Measurement:
    sr_actual = sf.info(path).samplerate           # the cap is in seconds, so use the file's real rate
    data, sr = sf.read(path, frames=int(max_seconds * sr_actual), dtype="float64", always_2d=True)
    peak = float(np.abs(data).max()) if data.size else 0.0
    rms = float(np.sqrt((data ** 2).mean())) if data.size else 0.0
    lufs = integrated_lufs(data, sr) if data.size else None
    silent = peak < 1e-5
    return Measurement(
        path=path, name=os.path.basename(path), seconds=round(len(data) / sr, 2), sample_rate=sr,
        channels=data.shape[1], lufs=None if silent else lufs, peak_db=db(peak),
        true_peak_db=db(peak) if silent else true_peak_db(data),
        rms_db=db(rms), crest_db=(db(peak) - db(rms)) if not silent else None,
        correlation=correlation(data), bands={} if silent else band_energy(data, sr), silent=silent,
    )
