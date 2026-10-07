"""Tests for the pose classifier (`gesture.poses`) and the filters/mapping it leans on.

Every hand here comes from `sources.fake.make_hand` — the same synthetic rig `gestured` itself is
exercised against via the fake UDP source — so a bug this finds is a bug in the classifier reading real
wire-format hands, not an artefact of a second hand-building path invented just for these tests.

`PoseClassifier` requires a pose to be held for `poses.DWELL_SECONDS` before it reports it (see the
docstring on `PoseClassifier.update`), so every "does this pose classify" test drives the classifier
across a span of simulated time rather than calling `update()` once — a single call on a fresh
classifier can never return anything but `NO_POSE`, by design.
"""
from __future__ import annotations

import pytest

from logic_mcp.gesture import filters, mapping, poses
from logic_mcp.gesture import protocol as P
from logic_mcp.gesture.sources.fake import make_hand


# --------------------------------------------------------------------------- helpers
def _hand_frame(pose: str, t: float, **kw) -> P.HandFrame:
    return P.HandFrame(t=t, hands=[make_hand(pose, **kw)], source="test")


def _hold(classifier: poses.PoseClassifier, make, duration: float, fps: float = 60.0) -> str:
    """Feed `make(t)`-built frames to `classifier` at `fps` for `duration` seconds and return the last
    classification — the thing every "this pose reads correctly" test actually cares about."""
    dt = 1.0 / fps
    steps = int(duration / dt)
    result = P.NO_POSE
    for i in range(steps):
        t = i * dt
        result = classifier.update(make(t), t)
    return result


def _hand_at(x: float, y: float) -> P.Hand:
    """A minimal hand whose only landmark that matters is the wrist — for exercising `CalibBox`
    directly without going through `make_hand`'s own 0..1 clamp on its `height`/`x` arguments."""
    return P.Hand(chirality="right", score=1.0, lm=[[x, y, 0.0] for _ in range(21)])


def _scaled_about_wrist(hand: P.Hand, factor: float) -> P.Hand:
    """Scale every landmark about the wrist by `factor` — simulates the same hand held closer to or
    farther from the camera, which is exactly what the scale-invariance tests need to hold constant."""
    wx, wy, wz = hand.lm[P.WRIST]
    lm = [[wx + (x - wx) * factor, wy + (y - wy) * factor, wz + (z - wz) * factor] for x, y, z in hand.lm]
    return P.Hand(chirality=hand.chirality, score=hand.score, lm=lm)


# --------------------------------------------------------------------------- the six poses
def test_open_palm_classifies():
    c = poses.PoseClassifier()
    assert _hold(c, lambda t: _hand_frame("open_palm", t), 0.3) == "open_palm"


def test_fist_classifies():
    c = poses.PoseClassifier()
    assert _hold(c, lambda t: _hand_frame("fist", t), 0.3) == "fist"


def test_point_classifies():
    c = poses.PoseClassifier()
    assert _hold(c, lambda t: _hand_frame("point", t), 0.3) == "point"


def test_pinch_classifies():
    """The clutch: this is the pose the spec asks to be the most reliable of the six, so it also gets
    the dedicated hysteresis latch in `PoseClassifier` — this test exercises that whole path, not just
    a raw geometry check."""
    c = poses.PoseClassifier()
    assert _hold(c, lambda t: _hand_frame("pinch", t), 0.3) == "pinch"


def test_swipe_left_classifies():
    """An open palm whose wrist actually travels left fast enough, for long enough, to look like a
    swipe rather than incidental drift. The first ~0.25 s reads as a held-still `open_palm` (there isn't
    enough motion history yet to estimate a velocity) before the classifier has enough history to see
    the sweep — that transient is expected, which is why this checks the pose after the whole motion,
    not on the first frame."""
    c = poses.PoseClassifier()

    def make(t: float) -> P.HandFrame:
        return _hand_frame("swipe_left", t, x=0.9 - 1.0 * t)   # ~1 span/sec leftward, well past threshold

    assert _hold(c, make, 0.6) == "swipe_left"


def test_spread_classifies():
    """Two hands, mirrored around the frame centre, pulling apart fast enough to read as a spread
    rather than two open palms that happen to share the frame."""
    c = poses.PoseClassifier()

    def make(t: float) -> P.HandFrame:
        x = 0.5 - 1.0 * t
        return P.HandFrame(t=t, hands=[
            make_hand("spread", x=x, chirality="right", score=0.95),
            make_hand("spread", x=1.0 - x, chirality="left", score=0.9),
        ], source="test")

    assert _hold(c, make, 0.6) == "spread"


def test_half_curled_hand_is_no_pose():
    """`make_hand(P.NO_POSE, ...)` builds the deliberately-ambiguous hand from the fixture module: four
    fingers half curled, thumb left alone. It must never resolve to any of the six poses, no matter how
    long it's held — fail closed, not a best guess."""
    c = poses.PoseClassifier()
    assert _hold(c, lambda t: _hand_frame(P.NO_POSE, t), 0.5) == P.NO_POSE


# --------------------------------------------------------------------------- scale invariance
@pytest.mark.parametrize("pose", ["open_palm", "fist", "pinch"])
def test_scale_invariance(pose):
    """The same pose, built once and then scaled 60% bigger about the wrist (a hand held closer to the
    camera), must classify identically — every finger test in `poses.py` divides by a distance that
    scales the same way, so this is really a test that none of them snuck in an absolute threshold."""
    small = make_hand(pose)
    big = _scaled_about_wrist(small, 1.6)

    c_small, c_big = poses.PoseClassifier(), poses.PoseClassifier()
    p_small = _hold(c_small, lambda t: P.HandFrame(t=t, hands=[small], source="test"), 0.3)
    p_big = _hold(c_big, lambda t: P.HandFrame(t=t, hands=[big], source="test"), 0.3)

    assert p_small == p_big == pose


# --------------------------------------------------------------------------- finger_states / pinch_gap
def test_finger_states_explains_point():
    fs = poses.finger_states(make_hand("point"))
    assert fs["index"] is True
    assert fs["middle"] is False and fs["ring"] is False and fs["pinky"] is False


def test_pinch_gap_is_monotonic_in_the_gap():
    gaps = [poses.pinch_gap(make_hand("pinch", gap=g)) for g in (0.0, 0.25, 0.5, 0.75, 1.0)]
    assert gaps == sorted(gaps)
    assert gaps[0] < gaps[-1]


# --------------------------------------------------------------------------- filters
def test_hysteresis_does_not_chatter_at_the_threshold():
    """Noise sitting right on `on_at` (but never dropping below `off_at`) must not flip the latch back
    and forth — that chatter is exactly what the two-threshold design exists to absorb."""
    h = filters.Hysteresis(on_at=0.5, off_at=0.3)
    readings = [0.51, 0.49, 0.51, 0.49, 0.52, 0.48]
    states = [h.update(v) for v in readings]
    assert all(states), "once latched on, dipping below on_at but staying above off_at must hold on"


def test_hysteresis_needs_the_off_threshold_to_release():
    h = filters.Hysteresis(on_at=0.5, off_at=0.3)
    assert h.update(0.6) is True
    assert h.update(0.4) is True        # below on_at, but not below off_at yet
    assert h.update(0.2) is False       # now past off_at: releases


def test_dwell_requires_the_full_duration():
    d = filters.Dwell(0.2)
    assert d.update(True, 0.00) is False
    assert d.update(True, 0.10) is False
    assert d.update(True, 0.19) is False
    assert d.update(True, 0.21) is True


def test_dwell_resets_the_instant_the_condition_breaks():
    d = filters.Dwell(0.2)
    assert d.update(True, 0.00) is False
    assert d.update(True, 0.19) is False
    assert d.update(False, 0.20) is False
    assert d.update(True, 0.21) is False        # held again, but only for 0 s so far
    assert d.update(True, 0.42) is True         # a fresh full 0.2 s from the restart


# --------------------------------------------------------------------------- mapping: unit conversions
@pytest.mark.parametrize("v", [0.02, 0.1, 0.35, 0.5, 0.75, 0.99, 1.0])
def test_db_round_trips(v):
    assert mapping.from_db(mapping.to_db(v)) == pytest.approx(v, abs=1e-9)


def test_db_silence_pole_round_trips_too():
    """Below `SILENCE_THRESHOLD`, `to_db` collapses to -inf on purpose (a real fader's bottom stop
    isn't a very quiet number, it's off). `from_db` maps -inf back to 0.0, and 0.0 is itself inside the
    collapsed region, so the round trip holds even across that many-to-one edge."""
    assert mapping.to_db(0.0) == float("-inf")
    assert mapping.from_db(float("-inf")) == 0.0
    assert mapping.to_db(mapping.from_db(float("-inf"))) == float("-inf")


@pytest.mark.parametrize("v", [0.0, 0.1, 0.33, 0.5, 0.9, 1.0])
def test_hz_round_trips(v):
    assert mapping.from_hz(mapping.to_hz(v)) == pytest.approx(v, abs=1e-9)


@pytest.mark.parametrize("pan", [-64, -32, 0, 1, 32, 63])
def test_pan_round_trips(pan):
    assert mapping.to_pan(mapping.from_pan(pan)) == pan


# --------------------------------------------------------------------------- mapping: CalibBox
def test_calibbox_clamps_outside_its_corners():
    box = mapping.CalibBox.from_corners((0.2, 0.2), (0.8, 0.8))
    assert box.map_x(_hand_at(-1.0, 0.5)) == 0.0
    assert box.map_x(_hand_at(5.0, 0.5)) == 1.0
    assert box.map_y(_hand_at(0.5, -1.0)) == 1.0   # above the box (smaller y) reads as the TOP: 1
    assert box.map_y(_hand_at(0.5, 5.0)) == 0.0    # below the box (larger y) reads as the BOTTOM: 0


def test_calibbox_bottom_of_box_is_zero_like_a_fader():
    box = mapping.CalibBox.from_corners((0.0, 0.2), (1.0, 0.8))
    assert box.map_y(_hand_at(0.5, 0.8)) == pytest.approx(0.0)   # bottom corner
    assert box.map_y(_hand_at(0.5, 0.2)) == pytest.approx(1.0)   # top corner
    assert box.map_y(_hand_at(0.5, 0.5)) == pytest.approx(0.5)   # halfway


# --------------------------------------------------------------------------- mapping: takeover
def test_takeover_jump_is_absolute():
    assert mapping.takeover(0.2, 0.9, "jump") == pytest.approx(0.9)


def test_takeover_catch_does_not_jump():
    """Soft takeover steps toward the target rather than snapping to it — a single call with the hand
    far from where the fader sits must land closer to `current` than to `target`, not on top of
    `target`."""
    result = mapping.takeover(0.2, 0.9, "catch")
    assert result != pytest.approx(0.9)
    assert abs(result - 0.2) <= mapping.CATCH_MAX_STEP + 1e-9


def test_takeover_catch_converges_with_repeated_calls():
    """Called every tick while engaged (as `gestured` does), catch mode should close the gap over
    successive calls rather than getting stuck."""
    value = 0.2
    for _ in range(200):
        value = mapping.takeover(value, 0.9, "catch")
    assert value == pytest.approx(0.9)


def test_takeover_unknown_mode_is_refused():
    with pytest.raises(ValueError):
        mapping.takeover(0.2, 0.9, "teleport")


def test_takeover_catch_is_rate_limited_per_second_when_given_dt():
    """2026-10-06: 0.05 per call at 120 Hz closed a 0.58 gap in a third of a second — a jump in all but
    name. With the tick length known the step is CATCH_RATE * dt, so a gap closes at the same speed no
    matter how fast the loop runs."""
    value = 0.0
    for _ in range(60):                                   # half a second at 120 Hz
        value = mapping.takeover(value, 1.0, "catch", dt=1 / 120)
    assert value == pytest.approx(min(1.0, 0.5 * mapping.CATCH_RATE), abs=1e-6)
    slow = 0.0
    for _ in range(30):                                   # the same half second at 60 Hz
        slow = mapping.takeover(slow, 1.0, "catch", dt=1 / 60)
    assert slow == pytest.approx(value, abs=1e-6)
