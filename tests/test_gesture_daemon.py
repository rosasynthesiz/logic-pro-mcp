"""The daemon wiring, end to end, with no camera, no microphone and no Logic.

Every other test in this stack checks one component. This one checks that they are actually plugged
together: a synthetic hand goes out over a real UDP socket, the daemon picks it up, the arbiter decides,
and a fake pad records the OSC that would have gone to Logic.

The pieces that are faked are exactly the two that need hardware — the pose classifier (a camera) and
the pad (Logic). Everything between them is the real code.
"""
from __future__ import annotations

import socket
import time

import pytest

from logic_mcp.gesture import protocol as P
from logic_mcp.gesture.gestured import Gestured
from logic_mcp.gesture.sources import fake

def _free_port() -> int:
    """Ask the OS for a port nobody is using, rather than picking a constant.

    Fixed test ports looked fine until two suite runs overlapped and one died with EADDRINUSE. UDP
    SO_REUSEADDR permits the rebind but not two live binds, so the only robust answer is to let the
    kernel choose.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakePad:
    """Records what would have gone to Logic. Shaped like LogicPad, backed by nothing."""

    def __init__(self, bank=None):
        self.sends: list[tuple[str, tuple]] = []
        self.presses: list[str] = []
        self._bank = bank or [{"n": n, "name": nm, "volume": 0.5, "pan": 0.5}
                              for n, nm in enumerate(["kick", "snare", "bass", "strings",
                                                      "string pad", "vox", "keys", "fx"], start=1)]

    def send(self, address, *args):
        self.sends.append((address, args))
        return True

    def press(self, address):
        self.presses.append(address)
        return True

    def snapshot(self):
        return {"bank": self._bank, "connected": True}

    def wait_for(self, address, timeout=1.5, since=None):
        return [1.0]

    @property
    def connected(self):
        return True


class ScriptedClassifier:
    """Stands in for the camera. `pose` is set by the test; `update` just reports it."""

    def __init__(self):
        self.pose = P.NO_POSE

    def update(self, frame, t):
        return self.pose


@pytest.fixture
def rig():
    H_PORT, V_PORT, HUD_PORT = _free_port(), _free_port(), _free_port()
    pad = FakePad()
    cls = ScriptedClassifier()
    g = Gestured(pad, sink=lambda verb, **kw: __import__(
        "logic_mcp.gesture.actions", fromlist=["Done"]).Done("confirmed", verb),
        classifier=cls, hands_port=H_PORT, voice_port=V_PORT, hud_port=HUD_PORT,
        verbose=False, takeover="jump")
    tx = P.sender(H_PORT)
    vx = P.sender(V_PORT)
    yield g, pad, cls, tx, vx, HUD_PORT
    g.close()
    tx.close()
    vx.close()


def pump(g, tx, cls, pose, height, t, n=3):
    """Push n frames of one pose/height through the real UDP socket and step the daemon."""
    cls.pose = pose
    out = []
    for i in range(n):
        tx.send(fake.frame(pose, height).to_json())
        time.sleep(0.01)
        out += g.step(t + i * 0.01)
    return out


def say(g, vx, text, t):
    vx.send(P.Utterance(t=t, text=text, final=True).to_json())
    time.sleep(0.01)
    return g.step(t)


# ---------------------------------------------------------------------------

def test_hand_alone_sends_nothing(rig):
    """Rule 1 end to end: a hand waving with nothing named must not produce a single OSC message."""
    g, pad, cls, tx, vx, HUD_PORT = rig
    pump(g, tx, cls, "pinch", 0.9, 1.0, n=6)
    assert pad.sends == []


def test_voice_then_clutch_rides_the_fader(rig, monkeypatch):
    """The whole Phase 3 story in one test: name it, grab it, move it."""
    g, pad, cls, tx, vx, HUD_PORT = rig
    from logic_mcp.gesture import protocol as PP
    monkeypatch.setattr("logic_mcp.gesture.gestured.grammar",
                        type("G", (), {"parse": staticmethod(
                            lambda s: PP.Intent(verb="volume", target="strings", raw=s))}))
    g.execute(say(g, vx, "volume on strings", 1.0))
    assert g.arb.state == P.ARMED
    assert pad.sends == []                                  # armed is not engaged

    g.execute(pump(g, tx, cls, "pinch", 0.8, 1.1, n=4))
    assert g.arb.state == P.ENGAGED
    assert pad.sends, "clutch closed but nothing was sent"
    addr, args = pad.sends[-1]
    assert addr == "/1/volume4"                              # "strings" is strip 4 on the fake bank
    assert 0.0 <= args[0] <= 1.0


def test_release_freezes_and_stops_sending(rig, monkeypatch):
    g, pad, cls, tx, vx, HUD_PORT = rig
    from logic_mcp.gesture import protocol as PP
    monkeypatch.setattr("logic_mcp.gesture.gestured.grammar",
                        type("G", (), {"parse": staticmethod(
                            lambda s: PP.Intent(verb="volume", target="strings", raw=s))}))
    g.execute(say(g, vx, "volume on strings", 1.0))
    g.execute(pump(g, tx, cls, "pinch", 0.8, 1.1, n=3))
    held = g.arb.binding.value
    n_before = len(pad.sends)

    g.execute(pump(g, tx, cls, P.NO_POSE, 0.1, 1.2, n=5))    # hand drops to the floor after release
    assert g.arb.state == P.FROZEN
    assert g.arb.binding.value == pytest.approx(held)        # held, not dragged to the bottom
    assert len(pad.sends) == n_before


def test_unknown_track_refuses_rather_than_guessing(rig, monkeypatch):
    """'string pad' and 'strings' both exist; an ambiguous prefix must not pick one."""
    g, pad, cls, tx, vx, HUD_PORT = rig
    from logic_mcp.gesture import protocol as PP
    monkeypatch.setattr("logic_mcp.gesture.gestured.grammar",
                        type("G", (), {"parse": staticmethod(
                            lambda s: PP.Intent(verb="volume", target="string", raw=s))}))
    g.execute(say(g, vx, "volume on string", 1.0))
    g.execute(pump(g, tx, cls, "pinch", 0.8, 1.1, n=3))
    assert pad.sends == []
    assert any(d.state == "failed" and "not on the surface" in d.detail for d in g.ex.log)


def test_resolve_exact_beats_prefix(rig):
    g, pad, cls, tx, vx, HUD_PORT = rig
    assert g.ex.resolve("strings") == 4
    assert g.ex.resolve("string pad") == 5
    assert g.ex.resolve("string") is None          # ambiguous: refuses
    assert g.ex.resolve("nothing") is None
    assert g.ex.resolve("3") == 3


def test_pose_fires_transport_when_idle(rig):
    g, pad, cls, tx, vx, HUD_PORT = rig
    g.execute(pump(g, tx, cls, "open_palm", 0.5, 1.0, n=2))
    assert "/1/play" in pad.presses


def test_hand_lost_freezes(rig, monkeypatch):
    """Stop sending frames entirely; the daemon must notice and freeze rather than hold engaged."""
    g, pad, cls, tx, vx, HUD_PORT = rig
    from logic_mcp.gesture import protocol as PP
    monkeypatch.setattr("logic_mcp.gesture.gestured.grammar",
                        type("G", (), {"parse": staticmethod(
                            lambda s: PP.Intent(verb="volume", target="bass", raw=s))}))
    g.execute(say(g, vx, "volume on bass", 1.0))
    g.execute(pump(g, tx, cls, "pinch", 0.7, 1.1, n=3))
    assert g.arb.state == P.ENGAGED
    t = 1.1 + 0.5                                   # past HandStream.LOST_AFTER with no datagrams
    g.execute(g.step(t))
    assert g.arb.state == P.FROZEN


def test_interim_utterances_are_ignored(rig, monkeypatch):
    """Acting on an interim hypothesis is how you mute the wrong track."""
    g, pad, cls, tx, vx, HUD_PORT = rig
    calls = []
    from logic_mcp.gesture import protocol as PP
    monkeypatch.setattr("logic_mcp.gesture.gestured.grammar",
                        type("G", (), {"parse": staticmethod(
                            lambda s: calls.append(s) or PP.Intent(verb="stop", raw=s))}))
    vx.send(PP.Utterance(t=1.0, text="sto", final=False).to_json())
    time.sleep(0.01)
    g.step(1.0)
    assert calls == []


def test_hud_publishes_logics_units(rig, monkeypatch):
    g, pad, cls, tx, vx, HUD_PORT = rig
    rx = P.UdpReceiver(HUD_PORT)
    try:
        g.publish(1.0)
        time.sleep(0.02)
        msgs = rx.drain()
        assert msgs and msgs[-1]["state"] == P.IDLE
    finally:
        rx.close()


# ---------------------------------------------------------------------------
# Soft takeover
# ---------------------------------------------------------------------------

def test_catch_takeover_converges_on_the_hand(rig, monkeypatch):
    """`mapping.takeover("catch")` is a per-call slew limiter, so it has to be fed the value it last
    produced. Feeding it the engage-time value every frame caps every move at one step forever: the
    fader creeps 0.05 and then sits there while the hand goes wherever it likes.
    """
    g, pad, cls, tx, vx, _ = rig
    g.takeover_mode = "catch"
    from logic_mcp.gesture import protocol as PP
    monkeypatch.setattr("logic_mcp.gesture.gestured.grammar",
                        type("G", (), {"parse": staticmethod(
                            lambda s: PP.Intent(verb="volume", target="strings", raw=s))}))
    g.execute(say(g, vx, "volume on strings", 1.0))

    # Engage with the hand well above where Logic's fader sits (the fake bank reports 0.5).
    t = 1.1
    g.execute(pump(g, tx, cls, "pinch", 0.95, t, n=2))
    assert g.arb.state == P.ENGAGED

    for i in range(60):                       # hold still; the fader should walk up to meet the hand
        t += 0.01
        g.execute(pump(g, tx, cls, "pinch", 0.95, t, n=1))

    values = [args[0] for _, args in pad.sends]
    assert len(set(values)) > 2, f"fader never moved past its first step: {sorted(set(values))[:5]}"
    assert max(values) > 0.6, f"fader stalled at {max(values):.3f} instead of catching the hand"


def test_catch_takeover_does_not_jump_on_engage(rig, monkeypatch):
    """The whole point: the first value sent must be near where Logic already is, not where the hand is."""
    g, pad, cls, tx, vx, _ = rig
    g.takeover_mode = "catch"
    from logic_mcp.gesture import protocol as PP
    monkeypatch.setattr("logic_mcp.gesture.gestured.grammar",
                        type("G", (), {"parse": staticmethod(
                            lambda s: PP.Intent(verb="volume", target="strings", raw=s))}))
    g.execute(say(g, vx, "volume on strings", 1.0))
    g.execute(pump(g, tx, cls, "pinch", 0.98, 1.1, n=1))
    first = pad.sends[0][1][0]
    assert abs(first - 0.5) <= 0.06, f"engage jumped straight to {first:.3f}; Logic was at 0.5"


# ---------------------------------------------------------------------------
# Calibration box from the command line
# ---------------------------------------------------------------------------

def test_parse_box_accepts_any_two_corners_and_refuses_junk():
    import argparse
    from logic_mcp.gesture import gestured as G
    box = G.parse_box("0.8,0.95,0.2,0.45")
    assert (box.x0, box.y0, box.x1, box.y1) == (0.2, 0.45, 0.8, 0.95)
    for bad in ("0.2,0.45,0.8", "0.2,0.45,0.8,1.5", "a,b,c,d"):
        with pytest.raises((argparse.ArgumentTypeError, ValueError)):
            G.parse_box(bad)
