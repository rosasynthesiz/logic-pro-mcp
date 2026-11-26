"""Regression cases for the Chinese Logic UI and Compressor Controls readout."""
from __future__ import annotations

from logic_mcp import plugins
from logic_mcp.channels import ax, ax_logic


def test_chinese_arrange_window_wins_over_other_standard_windows(monkeypatch):
    titles = {"marker": "Test - 标记列表", "tracks": "Test - 轨道"}
    monkeypatch.setattr(ax, "windows", lambda _: list(titles))
    monkeypatch.setattr(ax, "title", titles.get)
    assert ax_logic.main_window(None) == "tracks"


def test_compressor_parameters_use_the_label_in_the_same_cell(monkeypatch):
    root = {"AXRole": "AXWindow"}
    for name, display in (("Threshold：", "-31.0 dB"), ("Ratio:", "3.9:1"), ("Attack：", "10.0 ms")):
        slider = {"AXRole": "AXSlider", "AXValueDescription": display}
        readout = {"AXRole": "AXGroup", "AXValue": display, "AXChildren": [slider]}
        cell = {"AXRole": "AXCell", "AXChildren": [
            {"AXRole": "AXStaticText", "AXValue": name},
            readout,
            {"AXRole": "AXSlider", "AXValue": 12},
        ]}
        root.setdefault("AXChildren", []).append(cell)

    monkeypatch.setattr(ax, "attr", lambda el, key: el.get(key))
    assert plugins.resolve_control(root, "Threshold", "AXSlider")[0]["AXValueDescription"] == "-31.0 dB"
    assert plugins.resolve_control(root, "Ratio", "AXSlider")[0]["AXValueDescription"] == "3.9:1"
    assert plugins.resolve_control(root, "Attack", "AXSlider")[0]["AXValueDescription"] == "10.0 ms"

    root["AXChildren"].append(root["AXChildren"][0])
    assert plugins.resolve_control(root, "Threshold", "AXSlider") == (None, "ambiguous")
