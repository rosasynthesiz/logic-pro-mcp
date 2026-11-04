"""Exact targets: a mutation must land on the one track the caller named, or on none.

The old find_track returned the first track that matched EITHER selector, took the first of two tracks
with the same name, and substring-matched mutations. Every case below is one of the ways that wrote to
the wrong track. All UI is the fake tree in tests/fakeax.py; no Logic, MIDI, keys or sleep.
"""
from __future__ import annotations

import pytest

from logic_mcp import server
from logic_mcp.channels import ax_logic
from tests import fakeax


@pytest.fixture
def logic(monkeypatch):
    return lambda names=("Vocal", "Guitar", "Vocal", "Bass"), **kw: fakeax.wire(monkeypatch, names, **kw)


def _name(item):
    return ax_logic.track_info(item)["name"]


# ---------------------------------------------------------------- resolve_track
def test_index_alone_resolves_that_row(logic):
    L = logic()
    assert _name(ax_logic.resolve_track(L.win, index=2)) == "Guitar"


def test_unique_name_resolves(logic):
    L = logic()
    assert ax_logic.track_info(ax_logic.resolve_track(L.win, name="bass"))["index"] == 4


def test_duplicate_exact_names_are_refused_not_first_match(logic):
    L = logic()
    with pytest.raises(ax_logic.TargetError, match="2 tracks are named 'Vocal'"):
        ax_logic.resolve_track(L.win, name="Vocal")
    assert ax_logic.find_track(L.win, name="Vocal") is None


def test_conflicting_index_and_name_is_a_contradiction(logic):
    L = logic()
    with pytest.raises(ax_logic.TargetError, match="track 2 is named 'Guitar', not 'Vocal'"):
        ax_logic.resolve_track(L.win, index=2, name="Vocal")
    assert ax_logic.find_track(L.win, index=2, name="Vocal") is None


def test_agreeing_index_and_name_disambiguates_duplicates(logic):
    L = logic()
    assert ax_logic.track_info(ax_logic.resolve_track(L.win, index=3, name="Vocal"))["index"] == 3


@pytest.mark.parametrize("index", [0, -1, True, 1.0, "1"])
def test_bad_index_shapes_are_refused(logic, index):
    L = logic()
    with pytest.raises(ax_logic.TargetError, match="positive integer"):
        ax_logic.resolve_track(L.win, index=index)


@pytest.mark.parametrize("name", ["", "   ", 7])
def test_blank_or_non_string_name_is_refused(logic, name):
    L = logic()
    with pytest.raises(ax_logic.TargetError, match="non-blank"):
        ax_logic.resolve_track(L.win, name=name)


def test_no_selector_is_refused(logic):
    L = logic()
    with pytest.raises(ax_logic.TargetError, match="need index or name"):
        ax_logic.resolve_track(L.win)


def test_fuzzy_only_when_asked_and_unique(logic):
    L = logic(("Lead Vocal", "Guitar", "Bass"))
    with pytest.raises(ax_logic.TargetError, match="no track named 'vocal'"):
        ax_logic.resolve_track(L.win, name="vocal")                 # default: exact only
    assert _name(ax_logic.resolve_track(L.win, name="vocal", fuzzy=True)) == "Lead Vocal"
    L2 = logic(("Lead Vocal", "Backing Vocal"))
    with pytest.raises(ax_logic.TargetError, match="matches 2 tracks"):
        ax_logic.resolve_track(L2.win, name="vocal", fuzzy=True)


def test_missing_index_is_refused(logic):
    L = logic()
    with pytest.raises(ax_logic.TargetError, match="no track at index 9"):
        ax_logic.resolve_track(L.win, index=9)


# ---------------------------------------------------------------- the dispatcher refuses, and sends nothing
def _mute_boxes(L):
    return [c for it in ax_logic.track_items(L.win) for c in it.children if c.desc == "Mute"]


def test_mute_by_duplicate_name_is_refused_and_touches_nothing(logic):
    L = logic()
    r = server.logic_tracks("mute", name="Vocal", on=True)
    assert r["state"] == "failed" and "2 tracks are named" in r["reason"]
    assert all(b.pressed == 0 for b in _mute_boxes(L))
    L.midi.mcu_strip.assert_not_called()


def test_mute_with_contradicting_selectors_is_refused(logic):
    L = logic()
    r = server.logic_tracks("mute", index=1, name="Guitar", on=True)
    assert r["state"] == "failed" and "not 'Guitar'" in r["reason"]
    assert all(b.pressed == 0 for b in _mute_boxes(L))


def test_mutations_never_substring_match(logic):
    L = logic(("Lead Vocal", "Guitar"))
    r = server.logic_tracks("mute", name="Vocal", on=True)
    assert r["state"] == "failed" and "no track named" in r["reason"]
    assert all(b.pressed == 0 for b in _mute_boxes(L))


def test_mute_with_agreeing_selectors_lands_on_that_row_and_is_confirmed(logic):
    L = logic()
    r = server.logic_tracks("mute", index=3, name="Vocal", on=True)
    assert r["state"] == "confirmed", r
    boxes = _mute_boxes(L)
    assert [b.pressed for b in boxes] == [0, 0, 1, 0]
    assert boxes[2].value == 1


def test_set_volume_rejects_nonfinite_and_out_of_range(logic):
    L = logic()
    for bad in (float("nan"), float("inf"), 7.0, -97.0, True):
        r = server.logic_tracks("set_volume", index=1, db=bad)
        assert r["state"] == "failed" and "finite" in r["reason"], (bad, r)
    r = server.logic_tracks("set_pan", index=1, value=400)
    assert r["state"] == "failed"


def test_set_volume_minus_inf_is_still_a_legal_target(logic):
    L = logic()
    r = server.logic_tracks("set_volume", index=1, db=float("-inf"))
    assert r["state"] in ("confirmed", "uncertain")          # it reached the fader code, not the validator


# ---------------------------------------------------------------- project identity carried through the write
def test_project_switch_during_write_downgrades_confirmed(logic, monkeypatch):
    L = logic()
    real_press = L.ax.press

    def press_and_switch_project(el):
        ok = real_press(el)
        L.win.attrs["AXDocument"] = "file:///tmp/Other%20Song.logicx"
        L.win.title = "Other Song - Tracks"
        return ok
    monkeypatch.setattr(L.ax, "press", press_and_switch_project)
    r = server.logic_tracks("mute", index=2, name="Guitar", on=True)
    assert r["state"] == "uncertain"
    assert "front project changed" in r["reason"]
    assert r["project_changed"]["before"]["document"].endswith("Fixture%20Song.logicx")
    assert r["project_changed"]["now"]["document"].endswith("Other%20Song.logicx")


def test_settled_readback_requires_the_same_track_name(logic, monkeypatch):
    """Readback re-resolves by index AND name: if the row at that index is now another track, the write is
    not confirmed from its value."""
    L = logic()
    real_press = L.ax.press

    def press_and_rename_row(el):
        ok = real_press(el)
        items = ax_logic.track_items(L.win)
        items[1].desc = "Track 2 “Something Else”"       # the header at row 2 is no longer Guitar
        return ok
    monkeypatch.setattr(L.ax, "press", press_and_rename_row)
    r = server.logic_tracks("mute", index=2, name="Guitar", on=True)
    assert r["state"] == "uncertain", r


def test_project_identity_is_document_plus_title(logic):
    L = logic()
    assert ax_logic.project_identity(L.win) == {"document": "file:///tmp/Fixture%20Song.logicx", "title": "Fixture Song - Tracks"}
