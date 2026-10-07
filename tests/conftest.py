"""Shared fixtures.

Every test in this suite runs with Logic Pro CLOSED and no Accessibility grant. That is deliberate: the
modules under test are the ones that decide what is true, and they must be checkable on any machine and
in CI. Tests that genuinely need the app are marked `live` and excluded from the default run
(`uv run pytest -m live` opts back in).

Synthetic audio is generated here rather than committed as fixture files, so the repo stays small and the
signals stay self-documenting — you can read exactly what "speech-like" means below.
"""
from __future__ import annotations

import numpy as np
import pytest
import soundfile as sf

SR = 16000


def _write(tmp_path, name: str, x: np.ndarray, sr: int = SR) -> str:
    path = tmp_path / name
    sf.write(str(path), x.astype(np.float32), sr, subtype="FLOAT")
    return str(path)


def speechlike(seconds: float = 6.0, sr: int = SR, seed: int = 0) -> np.ndarray:
    """A voiced source with a GLIDING pitch, shaped into the formant band and gated into ~4 Hz syllables.

    This is not speech; it is the smallest signal that carries all four properties the detector keys on,
    and each one is here for a reason:

      * a harmonic (voiced) source           -> the pitch tracker in the strict music gate sees a real f0
      * an f0 that glides continuously       -> f0_hold lands mid-range, as speech does; a fixture built
                                                from noise scores 0.0 and a held tone scores 1.0, and both
                                                get thrown out as "music"
      * energy inside 300 Hz - 3.4 kHz       -> passes the absolute formant-band floor
      * ~4 Hz syllabic gating with consonant
        noise bursts at each onset           -> syllabic modulation and high spectral flux

    Built by phase integration so the pitch really moves, rather than by concatenating steady tones.
    """
    rng = np.random.default_rng(seed)
    n = int(seconds * sr)
    t = np.arange(n) / sr

    # gliding f0 around 130 Hz: a slow intonation contour plus a small random walk, as speech does
    walk = np.cumsum(rng.standard_normal(n)) / sr
    walk = walk / (np.abs(walk).max() + 1e-12)
    f0 = 130.0 * (1.0 + 0.30 * np.sin(2 * np.pi * 0.9 * t) + 0.15 * walk)
    phase = np.cumsum(f0) / sr
    voiced = sum(np.sin(2 * np.pi * k * phase) / k for k in range(1, 26))   # glottal-ish pulse train

    # consonant noise at each syllable onset — broadband, and what keeps spectral flux high
    syll_hz = 4.0
    syllable = (np.sin(2 * np.pi * syll_hz * t) > -0.2).astype(np.float64)
    onsets = np.concatenate([[0.0], np.maximum(0.0, np.diff(syllable))])
    burst = np.convolve(onsets, np.hanning(int(0.03 * sr)), mode="same") * rng.standard_normal(n)

    src = voiced / (np.abs(voiced).max() + 1e-12) + 0.35 * burst

    spec = np.fft.rfft(src)
    freqs = np.fft.rfftfreq(n, 1 / sr)
    spec[~((freqs >= 300) & (freqs <= 3400))] = 0.0        # formant band, language-independent by design
    band = np.fft.irfft(spec, n=n)
    band /= np.abs(band).max() + 1e-12

    env = np.convolve(syllable, np.hanning(int(0.02 * sr)), mode="same")
    env /= env.max() + 1e-12
    return (band * env * 0.3).astype(np.float32)


def musiclike(seconds: float = 6.0, sr: int = SR, seed: int = 1) -> np.ndarray:
    """A sustained tonal chord: stable pitches, steady spectrum, no syllabic gating.

    Low spectral-flux variance and a held f0 — what the music gate is built to reject.
    """
    n = int(seconds * sr)
    t = np.arange(n) / sr
    x = sum(np.sin(2 * np.pi * f * t) for f in (220.0, 277.2, 330.0, 440.0))
    x *= 1.0 + 0.02 * np.sin(2 * np.pi * 5.0 * t)      # gentle tremolo, nothing speech-like
    return (x / (np.abs(x).max() + 1e-12) * 0.3).astype(np.float32)


def silence(seconds: float = 2.0, sr: int = SR) -> np.ndarray:
    return np.zeros(int(seconds * sr), dtype=np.float32)


def sine(freq: float, seconds: float, sr: int = SR, amp: float = 1.0) -> np.ndarray:
    t = np.arange(int(seconds * sr)) / sr
    return (amp * np.sin(2 * np.pi * freq * t)).astype(np.float32)


@pytest.fixture
def speech_wav(tmp_path):
    return _write(tmp_path, "speech.wav", np.concatenate([silence(1.0), speechlike(6.0), silence(1.0)]))


@pytest.fixture
def music_wav(tmp_path):
    return _write(tmp_path, "music.wav", np.concatenate([silence(1.0), musiclike(6.0), silence(1.0)]))


@pytest.fixture
def write_wav(tmp_path):
    """Factory: write_wav("name.wav", samples, sr) -> path."""
    def _factory(name: str, x: np.ndarray, sr: int = SR) -> str:
        return _write(tmp_path, name, x, sr)
    return _factory
