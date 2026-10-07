"""Turning one hand (or two) into one of the six named poses, plus the continuous pinch axis.

Everything here reads `protocol.HandFrame`/`protocol.Hand` and returns plain values (a pose name, a
0..1 float, a dict of booleans) — no IO, no state shared with anything outside `PoseClassifier`, so it
is exercised entirely with `sources.fake.make_hand` fixtures and a clock the test drives by hand.

Two design rules run through the whole module:

1. **Scale invariance.** A hand twice as close to the camera is twice as big in frame, so no test here
   ever compares a raw landmark distance to a fixed number. Every distance is divided by the hand's own
   span (wrist to middle-finger MCP) before it is compared to anything, the same way the wire format's
   coordinates are normalised to the frame instead of to pixels.
2. **Fail closed.** A hand that doesn't clearly match one of the six shapes is `protocol.NO_POSE`, never
   a best guess. Guessing wrong here doesn't just misclassify a test fixture, it fires a real command at
   Logic — Stop, Undo, a fader move — from a hand that was reaching for the coffee cup.
"""
from __future__ import annotations

import math
from collections import deque

from . import filters
from . import protocol as P

# --------------------------------------------------------------------------- thresholds
# Below, "geometrically forced" means the sign or the >1/<1 boundary follows directly from what
# "extended" and "curled" mean; the exact number past that boundary is a guess that wants tuning
# against a real camera and a real hand, not derived from anything physical.

# An extended finger's tip is farther from the wrist than its own knuckle is, so the ratio must exceed
# 1.0 — that much is geometrically forced. Where the line sits above 1.0 was a guess until 2026-09-20,
# when it was measured against the real camera (MacBook Pro built-in, 30 fps):
#
#     extended finger   tip/pip  1.29 - 1.34      (synthetic fixtures: 1.33 - 1.41)
#     curled finger     tip/pip  0.19 - 0.55      (synthetic fixtures: 0.13)
#     half-curled       tip/pip  ~0.90            (the deliberately ambiguous fixture)
#
# The original single threshold of 1.3 sat exactly ON the extended cluster, so a genuinely open hand
# flickered on ordinary tracking noise and the pinky — lowest at 1.29 — read curled more often than not.
#
# A single threshold is the wrong shape regardless. The two clusters are far apart, and everything
# between them is a hand that is honestly mid-gesture. So there are two lines and a dead band: above
# EXTEND_RATIO is extended, below CURL_RATIO is curled, and in between the finger reports None and every
# pose that depends on it refuses. Fail closed, the same as the rest of this stack.
EXTEND_RATIO = 1.15
CURL_RATIO = 0.75

# The thumb doesn't curl toward the wrist along its own line the way the other four fingers do — it
# folds SIDEWAYS, across the palm, toward the index/middle side. So it gets its own reference point (the
# index MCP, a fixed spot on the palm) instead of the wrist, and its own guessed threshold, picked
# between the fixtures' open thumb (~0.69) and folded thumb (~0.52).
THUMB_EXTEND_RATIO = 0.6

# Pinch-gap hysteresis: the clutch is the most safety-critical pose in the vocabulary (it's what's
# allowed to touch a fader), so it gets a real two-threshold latch rather than a single number a jittery
# gap can sit right on top of. Both numbers are guesses; the GAP between them (not their absolute
# position) is what matters and is deliberately wide.
# MEASURED on 924 labelled frames of a real hand, 2026-09-20. The pinch gap turned out to be by far the
# cleanest signal in the whole vocabulary — the clusters do not even come close to touching:
#
#     pinch      gap  0.042 - 0.105
#     none       gap  0.162 - 0.231
#     fist       gap  0.362 - 0.544
#     open palm  gap  0.958 - 1.000
#
# The old 0.35/0.45 pair was catastrophically loose: it counted a relaxed half-curled hand (0.16-0.23)
# AND a fist (0.36-0.54) as "pinched". Nothing fired only because the finger tests then refused. These
# sit in the empty space between pinch and the next cluster up.
PINCH_ON = 0.14
PINCH_OFF = 0.22

# How long a candidate pose must hold before PoseClassifier reports it. A pose test that fires the
# instant its geometry lines up also fires on every transitional frame a hand passes through on its way
# to and from a real pose — a fist relaxing into an open palm spends a few frames looking like "index
# barely extended," and without a dwell that flickers a false `point` onto the bus. Guess: real dwell
# UX research runs 150-400 ms; this picks the low end so the clutch still feels responsive, and it
# wants tuning upward if real footage shows transitional false-fires.
DWELL_SECONDS = 0.15

# Motion-pose guesses: how fast, in "hand spans per second" (span-normalised, same reasoning as the
# extension ratios above), a wrist or a hand-to-hand separation has to change before it counts as a
# swipe or a spread rather than incidental drift while holding a static pose.
SWIPE_SPEED = 3.0            # spans/sec leftward
SPREAD_SPEED = 2.5           # spans/sec of separation growth
MOTION_WINDOW = 0.25         # seconds of history used to estimate either velocity
MOTION_MIN_DT = 0.03         # below this the velocity estimate is too noisy to trust
HISTORY_KEEP = 0.35          # how long motion history is retained before pruning


def _dist(a: list[float], b: list[float]) -> float:
    return math.dist(a, b)


def _span(hand: P.Hand) -> float:
    """Wrist-to-middle-MCP distance: the one measurement every scale-invariant test in this module
    divides by. It shrinks and grows with the hand's apparent size exactly as everything else does, so
    dividing by it cancels out how far the hand is from the camera."""
    return max(1e-6, _dist(hand.lm[P.WRIST], hand.lm[P.MIDDLE_MCP]))


def _extension_ratio(hand: P.Hand, tip_i: int, mcp_i: int) -> float:
    """tip-to-wrist distance over mcp-to-wrist distance. Both distances start from the wrist, so scaling
    the whole hand about the wrist (a change of camera distance) scales both equally and leaves the
    ratio untouched — that is what makes this test scale-invariant rather than just span-divided."""
    wrist = hand.lm[P.WRIST]
    tip_d = _dist(wrist, hand.lm[tip_i])
    mcp_d = max(1e-6, _dist(wrist, hand.lm[mcp_i]))
    return tip_d / mcp_d


def _thumb_extension_ratio(hand: P.Hand) -> float:
    """Distance from the thumb tip to the index MCP (a point ON the palm, not the wrist the other
    fingers curl toward), divided by span. A thumb resting across the palm is close to this point; a
    thumb splayed out to the side is not."""
    return _dist(hand.lm[P.THUMB_TIP], hand.lm[P.INDEX_MCP]) / _span(hand)


_FINGER_TIP_MCP = {
    "index": (P.INDEX_TIP, P.INDEX_MCP),
    "middle": (P.MIDDLE_TIP, P.MIDDLE_MCP),
    "ring": (P.RING_TIP, P.RING_MCP),
    "pinky": (P.PINKY_TIP, P.PINKY_MCP),
}


def finger_states(hand: P.Hand, extend_ratio: float = EXTEND_RATIO,
                   thumb_extend_ratio: float = THUMB_EXTEND_RATIO,
                   curl_ratio: float = CURL_RATIO) -> dict[str, bool | None]:
    """Per-finger state, so the HUD and tests can see *why* a pose fired rather than just what it decided.

    Three-valued on purpose: True extended, False curled, **None means neither** — the finger is in the
    dead band between the two and the hand is mid-gesture. Callers must test `is False` for "curled",
    never `not state`, or an ambiguous finger silently counts as a curled one and half a hand becomes a
    fist. Keys: thumb, index, middle, ring, pinky.
    """
    def tri(r: float, hi: float, lo: float) -> bool | None:
        return True if r > hi else False if r < lo else None

    states: dict[str, bool | None] = {
        name: tri(_extension_ratio(hand, tip, mcp), extend_ratio, curl_ratio)
        for name, (tip, mcp) in _FINGER_TIP_MCP.items()}
    # The thumb's own scale differs (it is measured across the palm, not along the wrist line), so its
    # dead band is scaled to its threshold rather than reusing the fingers' one.
    states["thumb"] = tri(_thumb_extension_ratio(hand), thumb_extend_ratio, thumb_extend_ratio * 0.75)
    return states


def pinch_gap(hand: P.Hand) -> float:
    """0..1, span-normalised distance between thumb tip and index tip. This is the continuous clutch
    axis (bound to a plug-in parameter when engaged), not just an input to the pinch pose test — the
    pose test latches it with hysteresis, but the raw value is exposed for anything that wants the
    unlatched signal."""
    return max(0.0, min(1.0, _dist(hand.lm[P.THUMB_TIP], hand.lm[P.INDEX_TIP]) / _span(hand)))


class PoseClassifier:
    """Six poses in, one name out. Call `update()` once per hand frame with a monotonically increasing
    timestamp (the same clock as `protocol.now()`); it holds all the state (motion history, the pinch
    latch, per-pose dwell timers) needed to turn one noisy frame at a time into a stable pose stream.

    Hysteresis is applied to `pinch_gap` specifically, because it is the one pose built on a genuinely
    continuous underlying signal that a jittery tracker can sit right on top of a single threshold —
    the clutch is also the pose this repo can least afford to chatter on. The other five poses are
    built from ratio tests that already have a wide margin in the fixtures (see the threshold comments
    above); they rely on `Dwell` alone to reject transitional frames rather than doubling up on latches
    for signals that aren't yet known to need one against real camera footage.
    """

    def __init__(self, *, extend_ratio: float = EXTEND_RATIO, thumb_extend_ratio: float = THUMB_EXTEND_RATIO,
                 pinch_on: float = PINCH_ON, pinch_off: float = PINCH_OFF,
                 swipe_speed: float = SWIPE_SPEED, spread_speed: float = SPREAD_SPEED,
                 dwell_seconds: float = DWELL_SECONDS):
        self.extend_ratio = extend_ratio
        self.thumb_extend_ratio = thumb_extend_ratio
        self.swipe_speed = swipe_speed
        self.spread_speed = spread_speed

        self._pinch_latch = filters.Hysteresis(on_at=-pinch_on, off_at=-pinch_off)
        self._dwells = {pose: filters.Dwell(dwell_seconds) for pose in P.POSES}
        self._wrist_hist: deque[tuple[float, float, float]] = deque()   # (t, wrist_x, span)
        self._sep_hist: deque[tuple[float, float, float]] = deque()     # (t, hand_separation, span)

    # ------------------------------------------------------------------ motion history
    @staticmethod
    def _prune(hist: deque, t: float) -> None:
        while hist and (t - hist[0][0]) > HISTORY_KEEP:
            hist.popleft()

    def _velocity(self, hist: deque, t: float, now_value: float) -> float | None:
        """Span-normalised rate of change over the last `MOTION_WINDOW` seconds, or None if there isn't
        enough history yet to trust an estimate."""
        if not hist:
            return None
        t0, v0, span0 = hist[0]
        for entry in hist:
            if t - entry[0] <= MOTION_WINDOW:
                t0, v0, span0 = entry
                break
        dt = t - t0
        if dt < MOTION_MIN_DT:
            return None
        span = max(1e-6, span0)
        return (now_value - v0) / dt / span

    # ------------------------------------------------------------------ per-frame classification
    def _raw_pose(self, frame: P.HandFrame, t: float) -> str:
        hand = frame.primary
        if hand is None:
            self._wrist_hist.clear()
            self._sep_hist.clear()
            return P.NO_POSE

        span = _span(hand)
        fs = finger_states(hand, self.extend_ratio, self.thumb_extend_ratio)
        pinched = self._pinch_latch.update(-pinch_gap(hand))

        wx = hand.lm[P.WRIST][0]
        self._wrist_hist.append((t, wx, span))
        self._prune(self._wrist_hist, t)

        two_hands = len(frame.hands) >= 2
        if two_hands:
            sep = _dist(frame.hands[0].lm[P.WRIST], frame.hands[1].lm[P.WRIST])
            self._sep_hist.append((t, sep, span))
            self._prune(self._sep_hist, t)
        else:
            self._sep_hist.clear()

        # Pinch first: it is the clutch, and the only pose whose signature (thumb+index closing while
        # the other three fingers stay put) cannot be produced by any other pose in this vocabulary.
        # Pinch is decided by the gap alone. Requiring the other three fingers to be *extended* as well
        # looked sensible and was measured wrong: on a real hand they sag while the thumb and index
        # close (measured p10 of 0.59 against an extend line of 1.15), so that clause rejected 100% of
        # genuine pinches. The gap on its own separates pinch from every other pose by a wide margin;
        # the fist guard below is belt-and-braces, since a fist's gap (0.36+) already fails the latch.
        if pinched and not all(fs[k] is False for k in ("index", "middle", "ring", "pinky")):
            return "pinch"

        # `is False` throughout: a finger in the dead band is not a curled finger, and treating it as
        # one turns every half-open hand into a fist.
        # Four fingers, not five. The thumb is excluded deliberately: it may be tucked inside the fist
        # or wrapped outside it, and on the real camera its ratio barely separates the two cases
        # (measured 0.76 closed vs 0.87 open, against a threshold of 0.60 — i.e. a real closed fist's
        # thumb reads *extended*). Requiring it made fists unclassifiable on a live hand while adding
        # nothing: no other pose in this vocabulary has all four fingers curled.
        if all(fs[k] is False for k in ("index", "middle", "ring", "pinky")):
            return "fist"

        if fs["index"] and all(fs[k] is False for k in ("middle", "ring", "pinky")):
            return "point"

        # Four fingers, no thumb — same reason the fist test drops it. With the thumb required, a real
        # open palm classified only 49% of the time; the four fingers alone are unambiguous, because
        # every other pose in this vocabulary leaves at least one of them curled or in the dead band.
        if all(fs[k] for k in ("index", "middle", "ring", "pinky")):
            # A second hand in frame means "spread" is the only motion this shape can resolve to — a
            # swipe is a one-handed gesture, and treating the primary hand's own leftward drift as a
            # swipe while a second hand is present would fire the wrong pose for exactly the case
            # (both hands pulling apart) this branch exists to catch.
            if two_hands:
                vel = self._velocity(self._sep_hist, t, sep)
                if vel is not None and vel > self.spread_speed:
                    return "spread"
                return "open_palm"
            vel = self._velocity(self._wrist_hist, t, wx)
            if vel is not None and vel < -self.swipe_speed:
                return "swipe_left"
            return "open_palm"

        return P.NO_POSE

    def update(self, frame: P.HandFrame, t: float) -> str:
        """Feed one frame, get back one of `protocol.POSES` or `protocol.NO_POSE`. Every candidate must
        hold for its full dwell window before it is reported — see `DWELL_SECONDS` — so a pose that
        fires instantly on the raw geometry would in practice fire constantly, once per transitional
        frame on the way through every other shape a hand passes through."""
        raw = self._raw_pose(frame, t)
        for pose in P.POSES:
            if self._dwells[pose].update(pose == raw, t):
                return pose
        return P.NO_POSE
