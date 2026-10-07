"""The arbiter state machine — the component that decides whether a hand may touch Logic at all.

This is the one piece of the gesture stack where a bug is expensive rather than annoying: the failure
modes are "fired Stop in the middle of a take" and "slammed the wrong track's fader to zero". So it was
built as a pure state machine with no IO, and it is tested exhaustively here with no camera, no
microphone and no Logic.

The three rules under test, in the order they matter:

1. No target, no motion. A hand waving with nothing named does nothing continuous.
2. Discrete poses are suppressed while the clutch is closed. Fingers curl during a fader ride; if `fist`
   still meant Stop there, every long ride would eventually stop the transport.
3. Releasing the clutch FREEZES the value. It never resets it. An accidental release is the most likely
   thing that happens in this system, and a snap to zero would be unforgivable.
"""
from __future__ import annotations

import pytest

from logic_mcp.gesture import protocol as P
from logic_mcp.gesture.arbiter import Arbiter


def kinds(actions):
    return [a.kind for a in actions]


def names(actions):
    return [a.name for a in actions]


def intent(verb, target=None, value=None, unit=None, axis=None):
    return P.Intent(verb=verb, target=target, value=value, unit=unit, axis=axis, raw="test")


@pytest.fixture
def arb():
    return Arbiter()


# ---------------------------------------------------------------------------
# Rule 1 — no target, no motion
# ---------------------------------------------------------------------------

def test_starts_idle(arb):
    assert arb.state == P.IDLE
    assert arb.target is None


def test_value_in_idle_does_nothing(arb):
    assert arb.on_value(0.9, 1.0) == []


def test_value_in_armed_does_nothing(arb):
    """Naming a track is not touching it. The axis stays dead until the clutch closes."""
    arb.on_intent(intent("volume", "strings"), 1.0)
    assert arb.state == P.ARMED
    assert arb.on_value(0.9, 1.1) == []


def test_clutch_without_a_target_is_refused(arb):
    out = arb.on_pose("pinch", 1.0)
    assert arb.state == P.IDLE
    assert names(out) == ["clutch_no_target"]


def test_value_flows_only_once_engaged(arb):
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    assert arb.state == P.ENGAGED
    out = arb.on_value(0.42, 1.2)
    assert len(out) == 1
    assert out[0].kind == "axis"
    assert out[0].axis == "volume"
    assert out[0].target == "strings"
    assert out[0].value == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# Rule 2 — poses are suppressed while engaged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("pose,kind,name", [
    ("open_palm", "transport", "play"),
    ("fist", "transport", "stop"),
    ("swipe_left", "key", "undo"),
    ("point", "select", "under_pointer"),
    ("spread", "key", "zoom"),
])
def test_poses_fire_when_not_engaged(arb, pose, kind, name):
    out = arb.on_pose(pose, 1.0)
    assert (out[0].kind, out[0].name) == (kind, name)


def test_fist_does_not_stop_transport_mid_ride(arb):
    """The defect this rule exists to prevent: curling a finger during a fader ride stopping the take."""
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    out = arb.on_pose("fist", 1.2)
    assert "transport" not in kinds(out)
    assert "suppressed" in names(out)


def test_pose_must_change_to_fire_again(arb):
    """Holding a pose is one command, not sixty a second."""
    first = arb.on_pose("fist", 1.0)
    again = arb.on_pose("fist", 1.1)
    assert names(first) == ["stop"]
    assert again == []


# ---------------------------------------------------------------------------
# Rule 3 — release freezes, never resets
# ---------------------------------------------------------------------------

def test_release_freezes_the_value(arb):
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    arb.on_value(0.73, 1.2)
    out = arb.on_pose(P.NO_POSE, 1.3)
    assert arb.state == P.FROZEN
    assert names(out) == ["frozen"]
    assert arb.binding.value == pytest.approx(0.73)      # held, not zeroed


def test_frozen_ignores_further_motion(arb):
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    arb.on_value(0.73, 1.2)
    arb.on_pose(P.NO_POSE, 1.3)
    assert arb.on_value(0.01, 1.4) == []
    assert arb.binding.value == pytest.approx(0.73)


def test_regrab_resumes_the_same_target(arb):
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    arb.on_value(0.73, 1.2)
    arb.on_pose(P.NO_POSE, 1.3)
    out = arb.on_pose("pinch", 1.5)
    assert arb.state == P.ENGAGED
    assert names(out) == ["engaged"]
    assert arb.target == "strings"


def test_losing_the_hand_freezes_rather_than_zeroing(arb):
    """A dropped camera frame must never read as 'the hand moved to the bottom of the box'."""
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    arb.on_value(0.68, 1.2)
    out = arb.on_hands_lost(1.3)
    assert arb.state == P.FROZEN
    assert arb.binding.value == pytest.approx(0.68)
    assert "frozen" in names(out)


# ---------------------------------------------------------------------------
# Explicit values bypass the hand entirely
# ---------------------------------------------------------------------------

def test_spoken_value_is_a_command_not_a_binding(arb):
    out = arb.on_intent(intent("volume", "vocals", value=-3.0, unit="dB"), 1.0)
    assert arb.state == P.IDLE
    assert out[0].kind == "command"
    assert out[0].value == -3.0
    assert out[0].unit == "dB"


def test_spoken_value_does_not_hijack_a_live_ride(arb):
    """'volume vocals to minus three' while riding strings must not steal the hand."""
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    arb.on_intent(intent("volume", "vocals", value=-3.0, unit="dB"), 1.2)
    assert arb.state == P.ENGAGED
    assert arb.target == "strings"


def test_axis_verb_without_a_target_refuses(arb):
    out = arb.on_intent(intent("volume"), 1.0)
    assert arb.state == P.IDLE
    assert names(out) == ["no_target"]


# ---------------------------------------------------------------------------
# Re-targeting
# ---------------------------------------------------------------------------

def test_retarget_while_idle_is_immediate(arb):
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_intent(intent("pan", "drums"), 1.1)
    assert arb.state == P.ARMED
    assert (arb.target, arb.axis) == ("drums", "pan")


def test_retarget_mid_ride_is_queued_not_applied(arb):
    """Swapping the binding under a closed clutch would jump a different fader to wherever the hand is."""
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    out = arb.on_intent(intent("volume", "drums"), 1.2)
    assert names(out) == ["queued"]
    assert arb.target == "strings"
    assert arb.pending == ("drums", "volume")


def test_queued_retarget_applies_on_release(arb):
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    arb.on_intent(intent("volume", "drums"), 1.2)
    out = arb.on_pose(P.NO_POSE, 1.3)
    assert names(out) == ["frozen", "armed"]
    assert arb.state == P.ARMED
    assert arb.target == "drums"
    assert arb.pending is None


# ---------------------------------------------------------------------------
# Timeouts
# ---------------------------------------------------------------------------

def test_frozen_expires_to_idle(arb):
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    arb.on_pose(P.NO_POSE, 1.2)
    assert arb.tick(1.2 + 3.9) == []
    out = arb.tick(1.2 + 4.1)
    assert arb.state == P.IDLE
    assert arb.binding is None
    assert names(out) == ["released"]


def test_armed_expires_to_idle(arb):
    arb.on_intent(intent("volume", "strings"), 1.0)
    assert arb.tick(1.0 + 9.5) == []
    arb.tick(1.0 + 10.5)
    assert arb.state == P.IDLE
    assert arb.binding is None


def test_engaged_never_expires(arb):
    """A slow, deliberate fader move is not a timeout. Only the clutch ends a ride."""
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    assert arb.tick(1000.0) == []
    assert arb.state == P.ENGAGED


# ---------------------------------------------------------------------------
# One-shot voice commands, and clamping
# ---------------------------------------------------------------------------

def test_one_shot_command_runs_in_any_state(arb):
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    out = arb.on_intent(intent("save"), 1.2)
    assert out[0].kind == "command" and out[0].name == "save"
    assert arb.state == P.ENGAGED          # voice is deliberate; it does not break the ride


@pytest.mark.parametrize("raw,want", [(-0.5, 0.0), (0.0, 0.0), (0.5, 0.5), (1.0, 1.0), (7.0, 1.0)])
def test_values_are_clamped(arb, raw, want):
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    out = arb.on_value(raw, 1.2)
    assert out[0].value == pytest.approx(want)


def test_history_records_transitions(arb):
    arb.on_intent(intent("volume", "strings"), 1.0)
    arb.on_pose("pinch", 1.1)
    arb.on_pose(P.NO_POSE, 1.2)
    assert [(f, t) for _, f, t in arb.history] == [
        (P.IDLE, P.ARMED), (P.ARMED, P.ENGAGED), (P.ENGAGED, P.FROZEN)]


def test_snapshot_shape(arb):
    arb.on_intent(intent("pan", "drums"), 1.0)
    s = arb.snapshot(1.0)
    assert s["state"] == P.ARMED and s["target"] == "drums" and s["axis"] == "pan"
    assert s["clutch"] is False
