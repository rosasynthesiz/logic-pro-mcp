"""Signal conditioning for a live hand: smoothing, latching, dwell and repeat suppression.

Nothing here knows about hands, poses or Logic — it operates on plain floats (and sequences of floats)
plus a timestamp, so it can be unit-tested with numbers alone and reused anywhere a noisy, bursty signal
needs to become a clean one.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field


# --------------------------------------------------------------------------- One Euro filter
class OneEuro:
    """The One Euro filter (Casiez, Roussel & Vogel, CHI 2012).

    A hand tracker's raw signal has two enemies that pull in opposite directions: jitter when the hand
    is nearly still, and lag when it moves fast. A fixed-window moving average can only trade one for
    the other — widen the window and jitter drops but a fast fader ride arrives late; narrow it and the
    ride is prompt but a resting hand vibrates on screen. One Euro instead adapts its own cutoff
    frequency to the signal's speed each sample: slow movement gets a low cutoff (heavy smoothing, kills
    jitter), fast movement gets a cutoff that rises toward `mincutoff + beta * |speed|` (almost no
    smoothing, kills lag). That is the whole trick, and it is why this beats a plain moving average for
    hand tracking specifically — the two failure modes never happen on the same frame, so there is no
    single window size that is right for both.

    `mincutoff` sets the cutoff at zero speed (lower = smoother at rest, but more lag on a slow ride).
    `beta` sets how fast the cutoff opens up with speed (higher = fast motion cuts through smoothing
    sooner, but a jittery fast signal stays jittery). `dcutoff` smooths the speed estimate itself, which
    is what actually derives the adaptive cutoff.
    """

    def __init__(self, freq: float = 60.0, mincutoff: float = 1.0, beta: float = 0.007,
                 dcutoff: float = 1.0):
        self.freq = freq
        self.mincutoff = mincutoff
        self.beta = beta
        self.dcutoff = dcutoff
        self._x_prev: float | None = None
        self._dx_prev: float = 0.0
        self._t_prev: float | None = None

    @staticmethod
    def _alpha(cutoff: float, dt: float) -> float:
        te = 1.0 / cutoff
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt) if dt > 0 else 1.0

    def filter(self, x: float, t: float) -> float:
        """Feed one sample at time `t` (same clock as `protocol.now()`) and get the smoothed value."""
        if self._t_prev is None:
            self._x_prev = x
            self._t_prev = t
            return x

        dt = max(1e-6, t - self._t_prev)
        freq = 1.0 / dt

        # Estimate speed from the raw signal, smoothed a little of its own accord (dcutoff) so a single
        # noisy sample doesn't spike the adaptive cutoff.
        dx = (x - self._x_prev) * freq
        a_d = self._alpha(self.dcutoff, dt)
        dx_hat = a_d * dx + (1 - a_d) * self._dx_prev

        cutoff = self.mincutoff + self.beta * abs(dx_hat)
        a = self._alpha(cutoff, dt)
        x_hat = a * x + (1 - a) * self._x_prev

        self._x_prev = x_hat
        self._dx_prev = dx_hat
        self._t_prev = t
        return x_hat


class OneEuroVec:
    """`OneEuro` applied element-wise across a fixed-length sequence (e.g. one 21x3 landmark set), so a
    whole hand's worth of coordinates goes through one object and one call instead of 63 of them."""

    def __init__(self, n: int, freq: float = 60.0, mincutoff: float = 1.0, beta: float = 0.007,
                 dcutoff: float = 1.0):
        self._filters = [OneEuro(freq, mincutoff, beta, dcutoff) for _ in range(n)]

    def filter(self, xs: list[float], t: float) -> list[float]:
        if len(xs) != len(self._filters):
            raise ValueError(f"expected {len(self._filters)} values, got {len(xs)}")
        return [f.filter(x, t) for f, x in zip(self._filters, xs)]


# --------------------------------------------------------------------------- latches
class Hysteresis:
    """A two-threshold latch: once ON, stays ON until the value drops below `off_at`; once OFF, stays
    OFF until it rises above `on_at`. Without the gap, a value sitting exactly on a single threshold
    (as a held pinch does — hands aren't steady to the millimetre) flips the latch every frame the
    tracker's noise nudges it either side, which is exactly the chatter a clutch cannot tolerate.
    """

    def __init__(self, on_at: float, off_at: float):
        if off_at > on_at:
            raise ValueError("off_at must be <= on_at: the off threshold is the lower one")
        self.on_at = on_at
        self.off_at = off_at
        self._on = False

    def update(self, value: float) -> bool:
        if self._on:
            if value < self.off_at:
                self._on = False
        else:
            if value > self.on_at:
                self._on = True
        return self._on

    @property
    def on(self) -> bool:
        return self._on


class Dwell:
    """A condition must hold continuously for `seconds` before `update()` reports True. The instant the
    condition breaks, the clock resets to zero — a dwell is "held the whole time", not "held on
    average", because a pose that flickers off for one frame and back on was never actually held.
    """

    def __init__(self, seconds: float):
        self.seconds = seconds
        self._since: float | None = None
        self._done = False
        self._last_t: float = 0.0

    def update(self, cond: bool, t: float) -> bool:
        self._last_t = t
        if not cond:
            self._since = None
            self._done = False
            return False
        if self._since is None:
            self._since = t
        self._done = (t - self._since) >= self.seconds
        return self._done

    @property
    def progress(self) -> float:
        """0..1 for the HUD: how far through the dwell window the current hold has got."""
        if self._since is None:
            return 0.0
        if self.seconds <= 0:
            return 1.0
        return max(0.0, min(1.0, (self._last_t - self._since) / self.seconds))


class Debounce:
    """Suppresses repeat firings of the same value within `window` seconds of the last time it fired.
    A different value is never suppressed — this guards against re-firing the SAME thing, not against
    firing quickly in general (a `swipe_left` right after a `swipe_right` should still go through).
    """

    def __init__(self, window: float):
        self.window = window
        self._last_value: object = _UNSET
        self._last_t: float | None = None

    def allow(self, value: object, t: float) -> bool:
        """Returns True (and records the firing) if `value` may fire now."""
        if self._last_t is not None and value == self._last_value and (t - self._last_t) < self.window:
            return False
        self._last_value = value
        self._last_t = t
        return True


class _Unset:
    def __eq__(self, other) -> bool:
        return False

    def __repr__(self) -> str:
        return "<unset>"


_UNSET = _Unset()
