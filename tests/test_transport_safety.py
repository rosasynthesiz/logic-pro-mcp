"""Transport regression tests: all application and hardware boundaries are mocked."""
from unittest.mock import Mock

import pytest

from logic_mcp import server


@pytest.fixture
def transport(monkeypatch):
    # Replace whole channel modules as well as dispatcher helpers so a regression
    # cannot accidentally touch Logic, Accessibility, MIDI, or AppleScript.
    for name in ("target", "ax", "ax_logic", "applescript", "keys", "doctor"):
        monkeypatch.setattr(server, name, Mock())
    midi = Mock()
    midi.mcu_button.return_value = True
    midi.mmc.return_value = True
    monkeypatch.setattr(server, "bus", Mock(return_value=midi))
    monkeypatch.setattr(server, "_win", Mock(return_value=None))
    monkeypatch.setattr(server, "_mcu_live", Mock(return_value=False))
    monkeypatch.setattr(server, "_key", Mock(return_value=True))
    monkeypatch.setattr(server, "transport_state", Mock())
    monkeypatch.setattr(server.time, "sleep", Mock())
    return midi


@pytest.mark.parametrize("playing", [None, 0, 1, "false", "true"])
def test_unknown_stop_never_sends_space(transport, playing):
    server.transport_state.return_value = {"playing": playing}
    result = server.logic_transport("stop")
    server._key.assert_not_called()
    transport.mmc.assert_called_once_with("stop")
    assert result["state"] == "uncertain"


@pytest.mark.parametrize("after", [None, True, 0])
def test_sent_mcu_stop_never_falls_through_to_toggle(transport, after):
    server._mcu_live.return_value = True
    server.transport_state.side_effect = [{"playing": True}, {"playing": after}]
    result = server.logic_transport("stop")
    transport.mcu_button.assert_called_once_with("stop")
    server._key.assert_not_called()
    transport.mmc.assert_not_called()
    assert result["state"] == "uncertain"


def test_space_stop_requires_confirmed_playing(transport):
    server.transport_state.side_effect = [{"playing": True}, {"playing": False}]
    result = server.logic_transport("stop")
    server._key.assert_called_once_with("play_stop")
    assert result["state"] == "confirmed"


def test_already_stopped_is_noop(transport):
    server.transport_state.return_value = {"playing": False}
    assert server.logic_transport("stop")["state"] == "confirmed"
    server._key.assert_not_called()
    transport.mcu_button.assert_not_called()
    transport.mmc.assert_not_called()


@pytest.mark.parametrize("action,field", [("play", "playing"), ("record", "recording")])
@pytest.mark.parametrize("current", [None, 0, 1, "true"])
def test_unknown_state_never_toggles(transport, action, field, current):
    server._win.return_value = object()
    server.transport_state.return_value = {field: current}
    assert server.logic_transport(action)["state"] == "uncertain"
    server.ax_logic.set_checkbox.assert_not_called()
    transport.mcu_button.assert_not_called()
    server._key.assert_not_called()


def test_already_recording_is_noop(transport):
    server._win.return_value = object()
    server.transport_state.return_value = {"playing": True, "recording": True}
    assert server.logic_transport("record")["state"] == "confirmed"
    server.ax_logic.set_checkbox.assert_not_called()
    transport.mcu_button.assert_not_called()
    server._key.assert_not_called()


@pytest.mark.parametrize("channel", ["ax", "mcu", "keys"])
@pytest.mark.parametrize("recording", [False, None, 1, True])
def test_record_confirmation_requires_recording_not_playback(transport, channel, recording):
    if channel == "ax":
        server._win.return_value = object()
        server.ax_logic.set_checkbox.return_value = {"how": "press"}
    elif channel == "mcu":
        server._mcu_live.return_value = True
    server.transport_state.side_effect = [
        {"playing": True, "recording": False},
        {"playing": True, "recording": recording},
    ]
    result = server.logic_transport("record")
    assert result["channel"] == channel
    assert result["state"] == ("confirmed" if recording is True else "uncertain")
    if channel != "keys":
        server._key.assert_not_called()
