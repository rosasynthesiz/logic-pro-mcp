"""What can be pinned down in logic_mcp.markers without Logic open or an Accessibility grant.

MAJOR DISCREPANCY from the brief for this file (flagged in full in the final report): markers.py has no
pure parsing/formatting helpers at all. There is no SMPTE string parser, no bar-beat parser, and no
marker-ordering function anywhere in this module or the rest of the package (checked with a repo-wide
grep). Every function here -- public (`read_markers`, `scene_cut_markers`, `go_to_smpte`,
`create_marker_here`) and private (`_marker_list_window`, `_focused_inside`) -- takes a live `root`
AXUIElement (or a `pid`) and drives Logic's menus/Accessibility tree directly:

* `read_markers` walks live AXRow/AXCell/AXGroup/AXSlider elements off a real Marker List window.
* `go_to_smpte` takes hours/minutes/seconds/frames as four separate integers and formats them straight
  into a keystroke string (`f"{hours:02d}{minutes:02d}{seconds:02d}{frames:02d}"`) sent via `keys.type_keys`
  -- there is no SMPTE *string* anywhere to parse or round-trip.
* the closest thing to "ordering a marker list" is `read_markers`'s `index = i + 1`, which is just the
  on-screen AXRow order Logic itself already sorted, not a computation this module performs.

The task's own rule for this file is "leave [Accessibility-touching things] untested (skip it, don't mock
Accessibility internals)" -- taken together with the above, that rule excludes the entire module's
behaviour. What follows is the small amount of real surface that doesn't require mocking anything: the
window-title constant the whole module keys off, that importing the module has no Accessibility/Logic
side effects (so it stays safe to collect in this Logic-closed, no-Accessibility-grant suite), and the
public call signatures, pinned so a signature drift here is caught here rather than at a live call site.
The rest are explicit skips naming exactly what would need to change for them to become testable.
"""
from __future__ import annotations

import inspect

import pytest

from logic_mcp import markers


def test_importing_markers_requires_no_accessibility_or_logic():
    # the import already happened above; this just makes the intent explicit and gives the property a name
    assert markers.__name__ == "logic_mcp.markers"


def test_marker_list_title_constant():
    # _marker_list_window() matches on "Marker" being IN the live window title, but read_markers() presses
    # Navigate > "Open Marker List" -- a typo in either string silently breaks the whole marker workflow
    # without raising, since press_menu_item/window lookups fail soft (see read_markers' `if w is None`).
    assert markers.MARKER_LIST_TITLE == "Marker List"


@pytest.mark.parametrize("name,params", [
    ("read_markers", ["root"]),
    ("scene_cut_markers", ["root"]),
    ("go_to_smpte", ["root", "pid", "hours", "minutes", "seconds", "frames"]),
    ("create_marker_here", ["root", "pid", "name"]),
])
def test_public_function_signatures(name, params):
    """Guards the documented calling convention -- in particular that go_to_smpte takes four separate
    integer fields rather than a formatted time string, which is easy to assume otherwise from its name."""
    sig = inspect.signature(getattr(markers, name))
    assert list(sig.parameters) == params


@pytest.mark.skip(reason="no SMPTE string parser exists in this module (or anywhere in logic_mcp) to "
                          "round-trip -- go_to_smpte takes hours/minutes/seconds/frames as separate ints "
                          "and formats them straight into a keystroke, it never parses a SMPTE string back")
def test_smpte_parse_format_round_trip():
    ...


@pytest.mark.skip(reason="no bar-beat parser/formatter exists in this module to round-trip")
def test_bar_beat_parse_format_round_trip():
    ...


@pytest.mark.skip(reason="marker ordering is Logic's own on-screen AXRow order, read verbatim by "
                          "read_markers() via a live Marker List window -- there is no ordering computed "
                          "in this module to test without Accessibility")
def test_marker_list_ordering():
    ...


@pytest.mark.skip(reason="the only place a raw string is handled is the keystroke built inside "
                          "go_to_smpte (root/pid required), and read_markers()/scene_cut_markers() fail "
                          "soft by returning [] / an `unsure`/`fail` result rather than raising on a "
                          "missing or malformed Marker List window -- both require a live root to exercise")
def test_malformed_input_handling():
    ...
