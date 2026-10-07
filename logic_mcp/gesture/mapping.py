"""Turning hand geometry into the 0..1 axis values the arbiter and OSC layer trade in, and turning
those 0..1 values into the units Logic actually shows on screen (dB, pan steps, Hz).

Nothing here touches a socket. `CalibBox` reads a `protocol.Hand`; everything past that is plain
arithmetic, so it is exercised with numbers, never with a camera.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from . import protocol as P


def _clamp01(v: float) -> float:
    return max(0.0, min(1.0, v))


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# --------------------------------------------------------------------------- calibration
@dataclass(slots=True)
class CalibBox:
    """The rectangle, in normalised camera coordinates, that maps to the full 0..1 travel of an axis.

    Hand-height-to-value is meaningless on its own: "0.4" from the camera could be a hand resting on the
    desk or a hand raised to the chin, depending entirely on where the camera sits, how far back the
    player is, and what counts as "all the way down" for them today. None of that is knowable in
    advance, so `CalibBox` is not a default worth guessing at — it is a required one-time step where the
    player shows the rig their own low point and high point (`from_corners`) before a session starts.
    A box built for one camera position or one chair height does not carry over to the next session.
    """
    x0: float
    y0: float
    x1: float
    y1: float

    @classmethod
    def from_corners(cls, p1: tuple[float, float], p2: tuple[float, float]) -> "CalibBox":
        """Build a box from two opposite corners in either order — whichever the user happened to
        plant first when calibrating."""
        (ax, ay), (bx, by) = p1, p2
        return cls(x0=min(ax, bx), x1=max(ax, bx), y0=min(ay, by), y1=max(ay, by))

    def map_y(self, hand: P.Hand) -> float:
        """0..1, clamped. The BOTTOM of the box is 0 and the top is 1 — a fader convention, the exact
        opposite of raw screen y (which grows downward). Reading this like screen coordinates would put
        a raised hand at the bottom of the fader, which is backwards for every user who has ever touched
        a mixer."""
        span = self.y1 - self.y0
        if span <= 0:
            return 0.0
        wy = hand.lm[P.WRIST][1]
        return _clamp01((self.y1 - wy) / span)

    def map_x(self, hand: P.Hand) -> float:
        """0..1, clamped, left of the box = 0."""
        span = self.x1 - self.x0
        if span <= 0:
            return 0.0
        wx = hand.lm[P.WRIST][0]
        return _clamp01((wx - self.x0) / span)


# --------------------------------------------------------------------------- roll (pan)
def roll(hand: P.Hand) -> float:
    """Wrist rotation as 0..1, for pan.

    Uses the vector from the index MCP to the pinky MCP rather than any single point, because that
    vector rotates with the palm regardless of where the hand sits in frame — a single-landmark angle
    would conflate position with orientation. Geometrically forced: `atan2` of that vector is the only
    rotation-invariant reading available from two points. A guess needing tuning against a real camera:
    which slice of the full turn counts as "all the way" pan-left vs pan-right — a comfortable wrist
    roll is nowhere near a full 180 degrees, so this maps a quarter-turn either side of neutral
    (palm facing the camera, fingers up) onto the full 0..1 range; a stiffer or looser range may fit a
    given user's wrist better.
    """
    ix, iy = hand.lm[P.INDEX_MCP][0], hand.lm[P.INDEX_MCP][1]
    px, py = hand.lm[P.PINKY_MCP][0], hand.lm[P.PINKY_MCP][1]
    angle = math.atan2(py - iy, px - ix)
    return _clamp01((angle + math.pi / 2) / math.pi)


# --------------------------------------------------------------------------- unit conversions
DB_MIN, DB_MAX = -60.0, 6.0
PAN_MIN, PAN_MAX = -64, 63
HZ_MIN, HZ_MAX = 20.0, 20000.0

# Below this the fader reads as silence rather than a very quiet number — matches how Logic's own fader
# treats its bottom few pixels as -inf rather than a legible dB value.
SILENCE_THRESHOLD = 0.02


def to_db(v: float) -> float:
    """0..1 onto -60..+6 dB, linear. `v` below `SILENCE_THRESHOLD` reads as -inf, not a very quiet
    number — matching a real fader's bottom stop."""
    v = _clamp01(v)
    if v < SILENCE_THRESHOLD:
        return float("-inf")
    return DB_MIN + v * (DB_MAX - DB_MIN)


def from_db(db: float) -> float:
    """The inverse of `to_db`. This exists because the daemon places the hand's starting point at
    Logic's CURRENT value on engage, never at 0 — without it every clutch would yank the fader to
    wherever it last happened to be instead of picking up from where the track actually sits, which is
    the one thing a takeover scheme exists to prevent."""
    if db is None or math.isinf(db):
        return 0.0
    return _clamp01((db - DB_MIN) / (DB_MAX - DB_MIN))


def to_pan(v: float) -> int:
    """0..1 onto -64..+63 (Logic's pan range: 128 integer steps, not symmetric around zero)."""
    v = _clamp01(v)
    return int(round(PAN_MIN + v * (PAN_MAX - PAN_MIN)))


def from_pan(pan: float) -> float:
    """The inverse of `to_pan`, for the same take-over-at-current-value reason as `from_db`."""
    return _clamp01((pan - PAN_MIN) / (PAN_MAX - PAN_MIN))


def to_hz(v: float) -> float:
    """0..1 onto 20 Hz..20 kHz, LOGARITHMICALLY — ears and EQ displays both work in octaves, so a linear
    mapping would spend most of the hand's travel above 10 kHz and almost none of it in the bass."""
    v = _clamp01(v)
    return HZ_MIN * (HZ_MAX / HZ_MIN) ** v


def from_hz(hz: float) -> float:
    """The inverse of `to_hz`, again so a clutch grabbing an EQ frequency starts from where the band
    already sits instead of snapping it to whatever the hand's raw height happens to read as."""
    hz = max(HZ_MIN, min(HZ_MAX, hz))
    return _clamp01(math.log(hz / HZ_MIN) / math.log(HZ_MAX / HZ_MIN))


# --------------------------------------------------------------------------- takeover
# Catch mode's speed in fader travel per SECOND: 1.0 means a full-travel gap closes in one second, the
# order a motorised fader takes. Sized per second, not per call, on purpose. The first version allowed
# 0.05 per call, and the daemon calls this at 120 Hz: a 0.58 gap closed in a third of a second — never a
# jump by construction, indistinguishable from one by eye (measured 2026-10-06, Drums 0.579 -> 0.000 in
# 0.33 s). Still a guess as to feel; tune it against a real hand.
CATCH_RATE = 1.0
# Per-call ceiling for callers that do not know their tick length.
CATCH_MAX_STEP = 0.05


def takeover(current: float, target: float, mode: str = "catch", dt: float | None = None) -> float:
    """Reconcile the hand's requested value with where the fader already sits.

    `"jump"` is absolute: the fader goes straight to `target`, full stop — the way a plain slider works,
    and exactly the behaviour that makes every clutch engage yank the fader to wherever the hand
    happened to be resting.

    `"catch"` is relative/soft takeover, the way a motorised hardware fader does it: rather than snap to
    `target`, it steps toward it by at most `CATCH_RATE * dt` per call (`CATCH_MAX_STEP` when the caller
    does not say how long its tick is). Called once per control-loop tick while engaged, that step limit
    is what turns a big, sudden gap between the hand and the fader into a brief catch-up instead of a
    jump — the same job hardware fader motors do physically when you flip from one mode to another.
    """
    current = _clamp01(current)
    target = _clamp01(target)
    if mode == "jump":
        return target
    if mode == "catch":
        step = CATCH_RATE * dt if dt is not None else CATCH_MAX_STEP
        delta = _clamp(target - current, -step, step)
        return _clamp01(current + delta)
    raise ValueError(f"unknown takeover mode: {mode!r}")
