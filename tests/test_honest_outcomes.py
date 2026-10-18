"""Routes that cannot observe their effect say so. `accepted: true` + `uncertain` means "the bytes left";
`confirmed` is reserved for a postcondition that was actually read back."""
from __future__ import annotations

from unittest.mock import Mock

import pytest

from logic_mcp import result, server
from logic_mcp.channels.midi import McuState
from tests import fakeax


def test_sent_is_uncertain_and_accepted():
    r = result.sent("midi", "note 60")
    assert r["state"] == result.UNCERTAIN and r["accepted"] is True and "no readback" in r["reason"]
    assert len({result.CONFIRMED, result.UNCERTAIN, result.FAILED}) == 3      # still three states


@pytest.fixture
def midi_tool(monkeypatch):
    b = Mock(); b.mcu = McuState()
    b.open_logic_in.return_value = True
    for m in ("note", "chord", "cc", "program", "pitch_bend", "aftertouch", "sysex", "mmc", "mcu_strip", "mcu_fader"):
        getattr(b, m).return_value = True
    monkeypatch.setattr(server, "bus", lambda: b)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    return b


@pytest.mark.parametrize("call", [
    dict(action="note", note=60), dict(action="chord", notes=[60, 64]), dict(action="sequence", steps=[{"note": 60}]),
    dict(action="cc", number=7, value=100), dict(action="program", number=3), dict(action="pitch_bend", value=8192),
    dict(action="aftertouch", value=10), dict(action="sysex", data=[1, 2, 3]),
    dict(action="mcu_strip", command="mute", strip=2), dict(action="mcu_fader", strip=1, value=12443),
])
def test_blind_sends_are_never_confirmed(midi_tool, call):
    r = server.logic_midi(**call)
    assert r["state"] == "uncertain" and r["accepted"] is True, r


def test_send_failure_is_failed(midi_tool):
    midi_tool.note.return_value = False
    assert server.logic_midi("note", note=60)["state"] == "failed"


@pytest.mark.parametrize("call,field", [
    (dict(action="note", note=200), "note"), (dict(action="note", note=60, velocity=-1), "velocity"),
    (dict(action="note", note=60, channel=17), "channel"), (dict(action="note", note=60, duration=float("nan")), "duration"),
    (dict(action="cc", number=7, value=1000), "value"), (dict(action="chord", notes=[60, 300]), "notes"),
    (dict(action="chord", notes=[True]), "notes"), (dict(action="mcu_fader", strip=1, value=20000), "value"),
    (dict(action="note", note=True), "note"),
])
def test_out_of_range_midi_is_refused_before_sending(midi_tool, call, field):
    r = server.logic_midi(**call)
    assert r["state"] == "failed" and field in r["reason"], r
    for m in ("note", "chord", "cc", "mcu_fader"):
        getattr(midi_tool, m).assert_not_called()


# ---------------------------------------------------------------- menu press, quit, launch
def test_press_menu_is_accepted_not_confirmed(monkeypatch):
    L = fakeax.wire(monkeypatch)
    monkeypatch.setattr(server.ax_logic, "press_menu_item", lambda root, *p: True)
    r = server.logic_system("press_menu", path=["Track", "New Audio Track"])
    assert r["state"] == "uncertain" and r["accepted"] is True


def test_quit_is_confirmed_only_when_the_process_is_gone(monkeypatch):
    L = fakeax.wire(monkeypatch)
    server.applescript.quit_app.return_value = (True, "")
    L.target.find.return_value = fakeax.logic_app(pid=4242)          # still running after the request
    r = server.logic_project("quit")
    assert r["state"] == "uncertain" and r["accepted"] is True and "save sheet" in r["fix"]
    L.target.find.return_value = fakeax.logic_app(pid=None)
    assert server.logic_project("quit")["state"] == "confirmed"


def test_launch_is_confirmed_only_when_running(monkeypatch):
    L = fakeax.wire(monkeypatch)
    server.applescript.activate.return_value = (True, "")
    L.target.find.return_value = fakeax.logic_app(pid=None)
    r = server.logic_project("launch")
    assert r["state"] == "uncertain" and r["accepted"] is True
    L.target.find.return_value = fakeax.logic_app(pid=9)
    assert server.logic_project("launch")["state"] == "confirmed"


# ---------------------------------------------------------------- no blind retry through another channel
def test_unsettled_mcu_strip_toggle_does_not_fall_through_to_ax(monkeypatch):
    L = fakeax.wire(monkeypatch)
    L.midi.strip_for.return_value = 0
    L.midi.mcu_strip.return_value = True                                   # sent, and Logic never applies it
    monkeypatch.setattr(server, "_mcu_live", lambda *a, **k: True)
    r = server.logic_tracks("mute", index=1, name="Vocal", on=True)
    assert r["state"] == "uncertain" and r["accepted"] is True
    mute_box = [c for c in server.ax_logic.track_items(L.win)[0].children if c.desc == "Mute"][0]
    assert mute_box.pressed == 0                                            # the header checkbox was NOT pressed


def test_mcu_strip_not_sent_may_use_ax(monkeypatch):
    L = fakeax.wire(monkeypatch)
    L.midi.strip_for.return_value = 0
    L.midi.mcu_strip.return_value = False                                  # nothing left the process
    monkeypatch.setattr(server, "_mcu_live", lambda *a, **k: True)
    r = server.logic_tracks("mute", index=1, name="Vocal", on=True)
    assert r["state"] == "confirmed" and r["channel"] == "ax"


# ---------------------------------------------------------------- OSC status is a read
def test_osc_status_never_binds(monkeypatch):
    monkeypatch.setattr(server, "_osc_ready", Mock(side_effect=AssertionError("status must not call _osc_ready")))
    monkeypatch.setattr(server, "_osc_dev", None)
    r = server.logic_osc("status")
    assert r["state"] == "failed" and r["fix"] == "logic_osc(action='connect')"
    dev = Mock(); dev.connected = True; dev.snapshot.return_value = {"bank": []}
    monkeypatch.setattr(server, "_osc_dev", dev)
    r = server.logic_osc("status")
    assert r["state"] == "confirmed" and r["readback"] == {"bank": []}
    dev.ensure_page.assert_not_called(); dev.refresh.assert_not_called()


def test_osc_values_must_be_finite_unit_interval(monkeypatch):
    dev = Mock(); dev.connected = True
    monkeypatch.setattr(server, "_osc_ready", lambda *a, **k: (dev, None))
    for bad in (float("nan"), 1.5, -0.1, True):
        assert server.logic_osc("volume", strip=1, value=bad)["state"] == "failed"
        assert server.logic_osc("master_volume", value=bad)["state"] == "failed"
        assert server.logic_osc("pan", strip=1, value=bad)["state"] == "failed"
    dev.send.assert_not_called()


# ---------------------------------------------------------------- transport argument bounds
@pytest.mark.parametrize("tempo", [float("nan"), float("inf"), 4, 991, True, None])
def test_set_tempo_rejects_bad_numbers(monkeypatch, tempo):
    L = fakeax.wire(monkeypatch)
    r = server.logic_transport("set_tempo", tempo=tempo)
    assert r["state"] == "failed" and "tempo" in r["reason"]
    assert not any(e[0] == "set" for e in L.ax.log)


def test_set_tempo_reads_back(monkeypatch):
    L = fakeax.wire(monkeypatch)
    r = server.logic_transport("set_tempo", tempo=97.5)
    assert r["state"] == "confirmed" and r["readback"]["after"] == 97.5
