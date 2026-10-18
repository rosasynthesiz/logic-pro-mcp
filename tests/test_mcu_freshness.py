"""MCU evidence: a field is known only once Logic has sent it, and only post-write traffic for THAT field
confirms a write. Callbacks are replayed into the parser directly; no CoreMIDI port is opened."""
from __future__ import annotations

import time
from unittest.mock import Mock

import pytest

from logic_mcp import server
from logic_mcp.channels import midi
from logic_mcp.channels.midi import MCU_BUTTONS, McuState, MidiBus


def _bus() -> MidiBus:
    b = MidiBus.__new__(MidiBus)                    # no rtmidi objects: __init__ is skipped on purpose
    b._out = b._mcu_out = b._mcu_in = None
    b.mcu = McuState(); b._lcd = [" "] * 112
    import threading
    b._lock = threading.Lock(); b.rx_log = []; b.bank = None
    return b


def led(name: str, on: bool) -> list[int]:
    return [0x90, MCU_BUTTONS[name], 0x7F if on else 0x00]


# ---------------------------------------------------------------- unknown until seen
def test_unseen_leds_are_unknown_not_false():
    s = McuState()
    assert s.playing is None and s.recording is None and s.led("click") is None
    assert s.to_dict()["playing"] is None


def test_led_arrives_then_known():
    b = _bus()
    b._on_mcu((led("play", True), 0.0))
    assert b.mcu.playing is True and b.mcu.recording is None
    b._on_mcu((led("play", False), 0.0))
    assert b.mcu.playing is False


def test_stale_led_is_unknown_past_max_age():
    b = _bus()
    b._on_mcu((led("play", True), 0.0))
    b.mcu.seen_at["led:play"] = time.time() - 60
    assert b.mcu.led("play") is True                 # no age limit: last known value
    assert b.mcu.led("play", max_age=30) is None     # too old to act on


def test_unrelated_traffic_does_not_freshen_a_field():
    b = _bus()
    b._on_mcu((led("play", True), 0.0))
    t_play = b.mcu.seen_at["led:play"]
    t0 = time.time()
    time.sleep(0.01)
    b._on_mcu(([0xB0, 0x40, 0x30], 0.0))             # timecode digit
    b._on_mcu(([0xE3, 0x10, 0x20], 0.0))             # fader echo on strip 3
    assert b.mcu.last_rx > t0                         # the surface is alive...
    assert b.mcu.seen_at["led:play"] == t_play        # ...but the play LED is exactly as old as it was
    assert not b.mcu.observed_after("led:play", t0)
    assert b.mcu.observed_after("fader:3", t0)


def test_observed_after_is_per_field():
    b = _bus()
    t0 = time.time()
    time.sleep(0.01)
    b._on_mcu((led("record", True), 0.0))
    assert b.mcu.observed_after("led:record", t0)
    assert not b.mcu.observed_after("led:play", t0)
    assert not b.mcu.observed_after("led:record", time.time() + 1)


# ---------------------------------------------------------------- malformed callbacks
@pytest.mark.parametrize("event", [([], 0.0), (None, 0.0), "junk", 42, ([0x90, "x", 1], 0.0)])
def test_malformed_callbacks_are_counted_and_do_not_raise(event):
    b = _bus()
    b._on_mcu(event)
    assert b.mcu.playing is None and b.mcu.malformed == 1 and b.mcu.rx_count == 0


@pytest.mark.parametrize("msg", [[0xF0], [0x90, 0x5E], [0x90], [0xF0, 0x00, 0x00, 0x66, 0x14, 0x12]])
def test_short_messages_change_no_state(msg):
    b = _bus()
    b._on_mcu((msg, 0.0))
    assert b.mcu.playing is None and b.mcu.leds == {} and b.mcu.handshake is False
    assert b.mcu.rx_count == 1 and b.mcu.malformed == 0


def test_empty_message_is_not_evidence_of_life():
    b = _bus()
    b._on_mcu(([], 0.0))
    assert b.mcu.last_rx == 0.0 and b.mcu.malformed == 1


def test_handshake_replies_are_skipped_without_a_port():
    b = _bus()
    b._on_mcu(([0xF0, 0x00, 0x00, 0x66, 0x14, 0x02, 0x01, 0xF7], 0.0))
    assert b.mcu.handshake is True                    # state updated, no AttributeError on the missing port


def test_strip_leds_carry_their_own_timestamps():
    b = _bus()
    t0 = time.time(); time.sleep(0.01)
    b._on_mcu(([0x90, 0x10 + 2, 0x7F], 0.0))          # mute LED, strip 2
    assert b.mcu.strips[2]["mute"] is True
    assert b.mcu.observed_after("strip:2:mute", t0) and not b.mcu.observed_after("strip:2:solo", t0)


# ---------------------------------------------------------------- transport_state merges without inventing
@pytest.fixture
def ts(monkeypatch):
    b = Mock(); b.mcu = McuState()
    monkeypatch.setattr(server, "bus", lambda: b)
    win = object()
    monkeypatch.setattr(server, "_win", lambda: win)
    snap = {"play": True, "record": False, "tempo": 100.0, "position": None, "cycle": None}
    monkeypatch.setattr(server.ax_logic, "transport_snapshot", lambda w: dict(snap))
    return b, snap


def test_mcu_live_but_led_unseen_defers_to_ax(ts):
    b, snap = ts
    b.mcu.handshake = True; b.mcu.last_rx = time.time()
    s = server.transport_state()
    assert "mcu" in s["sources"] and s["playing"] is True and s["recording"] is False   # AX values, not False-by-default


def test_fresh_mcu_led_wins_over_ax(ts):
    b, snap = ts
    b.mcu.handshake = True; b.mcu.last_rx = time.time()
    b.mcu.leds["play"] = False; b.mcu.seen_at["led:play"] = time.time()
    assert server.transport_state()["playing"] is False


def test_nothing_reported_is_none(ts, monkeypatch):
    b, snap = ts
    monkeypatch.setattr(server.ax_logic, "transport_snapshot", lambda w: {"play": None, "record": None})
    s = server.transport_state()
    assert s["playing"] is None and s["recording"] is None


def test_passive_read_never_opens_ports(ts):
    b, _ = ts
    s = server.transport_state(passive=True)
    b.wait_ready.assert_not_called(); b.open_mcu.assert_not_called()
    assert s["sources"] == ["ax"]
    assert not server._mcu_live(open_ports=False)
    b.wait_ready.assert_not_called()


def test_write_path_may_open_ports(ts):
    b, _ = ts
    server._mcu_live()
    b.wait_ready.assert_called_once()


def test_transport_resource_is_passive(ts):
    b, _ = ts
    server.res_transport()
    b.wait_ready.assert_not_called(); b.open_mcu.assert_not_called()


# ---------------------------------------------------------------- post-write evidence in the dispatchers
@pytest.fixture
def mcu_tool(monkeypatch):
    b = Mock(); b.mcu = McuState(); b.mcu.handshake = True; b.mcu.last_rx = time.time()
    b.mcu_button.return_value = True
    monkeypatch.setattr(server, "bus", lambda: b)
    monkeypatch.setattr(server.time, "sleep", lambda s: None)
    monkeypatch.setattr(server, "_win", lambda: None)
    monkeypatch.setattr(server, "_key", lambda c: False)
    return b


def test_raw_mcu_button_is_confirmed_only_by_its_own_fresh_led(mcu_tool):
    b = mcu_tool
    r = server.logic_midi("mcu_button", command="cycle")
    assert r["state"] == "uncertain" and r.get("accepted") is True          # live surface, no LED: not proof

    b.mcu.leds["cycle"] = True; b.mcu.seen_at["led:cycle"] = time.time() - 5  # a cached LED is not proof either
    r = server.logic_midi("mcu_button", command="cycle")
    assert r["state"] == "uncertain"

    def press_then_led(name, hold=0.05):
        b.mcu.leds["cycle"] = False; b.mcu.seen_at["led:cycle"] = time.time() + 0.001
        return True
    b.mcu_button.side_effect = press_then_led
    r = server.logic_midi("mcu_button", command="cycle")
    assert r["state"] == "confirmed" and "LED False" in r["detail"]


def test_raw_mcu_button_without_handshake_fails(mcu_tool):
    b = mcu_tool
    b.mcu_button.return_value = False
    r = server.logic_midi("mcu_button", command="play")
    assert r["state"] == "failed" and "handshake" in r["reason"]


def test_metronome_toggle_needs_fresh_click_led(mcu_tool):
    b = mcu_tool
    r = server.logic_transport("toggle_metronome")
    assert r["state"] == "uncertain" and r.get("accepted") is True

    def press_then_led(name, hold=0.05):
        b.mcu.leds["click"] = True; b.mcu.seen_at["led:click"] = time.time() + 0.001
        return True
    b.mcu_button.side_effect = press_then_led
    r = server.logic_transport("toggle_metronome")
    assert r["state"] == "confirmed" and r["readback"] == {"click_led": True}
