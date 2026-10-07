"""Markers for picture work: scene-cut markers from the movie, markers at given times, and reading the list.

Everything here is menu-driven through Accessibility; nothing is typed unless the receiving field is
proven focused (a stray character in Logic's main window is a key command).
"""
from __future__ import annotations

import re
import time

from .channels import ax, ax_logic, dialogs, keys
from .result import fail, ok, unsure

MARKER_LIST_TITLE = "Marker List"


def _marker_list_window(root):
    for w in ax.windows(root):
        t = str(ax.title(w) or "")
        if "Marker" in t and t.endswith("Tracks") is False and "- Tracks" not in t:
            return w
    return None


def read_markers(root) -> list[dict]:
    """Open the Marker List (Navigate > Open Marker List) and read every row.

    The rows carry nothing in AXValue: Logic exposes each field as an AXDescription instead -- the name on
    an AXCell, and the position/length as an AXGroup whose description is the bar display ("5 1 1 1 ") over
    four AXSlider segments (bar, beat, division, tick). Reading values here returns 29 empty rows.

    Names come back for every row, but Logic only populates position/length for rows that are actually
    scrolled into view, so offscreen markers report position None. Resize or scroll the list to read them.
    """
    w = _marker_list_window(root)
    if w is None:
        ax_logic.press_menu_item(root, "Navigate", "Open Marker List")
        w = dialogs.wait_for(lambda: _marker_list_window(root), 3.0)
    if w is None:
        return []
    out = []
    for i, row in enumerate(ax.find_all(w, lambda e: ax.role(e) == "AXRow", max_depth=8, limit=500)):
        name, positions = None, []
        for _, el in ax.walk(row, 5):
            d = ax.desc(el)
            if not d:
                continue
            if ax.role(el) == "AXCell" and name is None:
                name = d
            elif ax.role(el) == "AXGroup":
                segs = [ax.value(s) for _, s in ax.walk(el, 2) if ax.role(s) == "AXSlider"]
                positions.append({"text": d.strip(), "segments": segs})
        pos = positions[0] if positions else None
        length = positions[1] if len(positions) > 1 else None
        out.append({
            "index": i + 1,
            "name": name,
            "position": pos["text"] if pos else None,
            "bar": (pos["segments"][0] if pos and pos["segments"] else None),
            "length": length["text"] if length else None,
        })
    return out


def scene_cut_markers(root) -> dict:
    """Navigate > Other > Create Movie Scene Cut Markers -- Logic's own cut detection on the project movie."""
    before = len(read_markers(root))
    if not ax_logic.press_menu_item(root, "Navigate", "Other", "~^Create Movie Scene Cut Markers"):
        return fail("markers", "Create Movie Scene Cut Markers not in the Navigate > Other menu (no movie in project?)")
    # Logic may show a progress sheet while it analyses the movie
    time.sleep(2.0)
    for _ in range(60):
        if not dialogs.other_windows(root, main_suffix="- Tracks") or _marker_list_window(root) is not None:
            break
        time.sleep(1.0)
    after = read_markers(root)
    made = len(after) - before
    if made > 0:
        return ok("markers", f"{made} scene-cut markers created", {"before": before, "after": len(after)})
    return unsure("markers", "no new markers visible in the Marker List", {"before": before, "after": len(after)})


def _focused_inside(root, dlg):
    """Logic's own focused element, only if it is a text field inside the dialog's frame."""
    import ApplicationServices as AS
    from .channels import screen
    err, el = AS.AXUIElementCopyAttributeValue(root, "AXFocusedUIElement", None)
    # The dialog's editable positions are AXGroups of numeric segments (value "\t1\t1\t1\t1"), not text fields.
    if err or el is None:
        return None
    if ax.role(el) not in ("AXTextField", "AXTextArea", "AXIncrementor", "AXGroup"):
        return None
    if ax.role(el) == "AXGroup" and not any(c.isdigit() for c in str(ax.value(el) or "")):
        return None
    fr, dr = screen.element_rect(el), screen.element_rect(dlg)
    if fr is None or dr is None:
        return None
    inside = dr[0] - 2 <= fr[0] and fr[0] + fr[2] <= dr[0] + dr[2] + 2 and dr[1] - 2 <= fr[1] and fr[1] + fr[3] <= dr[1] + dr[3] + 2
    return el if inside else None


def go_to_smpte(root, pid: int, hours: int, minutes: int, seconds: int, frames: int) -> bool:
    """Navigate > Go To > Position..., then type into the SMPTE field only if Logic reports it focused.

    SMPTE here is absolute project time: Logic projects usually start at 01:00:00:00, so a cut 5 s into
    the movie is 01:00:05:00 unless the project's start time was changed.
    """
    if not ax_logic.press_menu_item(root, "Navigate", "Go To", "~Position"):
        return False
    dlg = ax_logic.wait_window(root, "Go To Position", 3.0)
    if dlg is None:
        return False
    if _focused_inside(root, dlg) is None:
        ax_logic.close_window(root, "Go To Position")
        return False
    keys.press("tab", pid); time.sleep(0.15)            # bar/beat group -> SMPTE group on the New row
    if _focused_inside(root, dlg) is None:
        ax_logic.close_window(root, "Go To Position")
        return False
    keys.type_keys(f"{hours:02d}{minutes:02d}{seconds:02d}{frames:02d}", pid); time.sleep(0.1)
    keys.press("return", pid); time.sleep(0.5)
    if ax_logic.find_window(root, "Go To Position") is not None:
        ax_logic.close_window(root, "Go To Position")
        return False
    return True


def create_marker_here(root, pid: int, name: str | None = None) -> dict:
    """Navigate > Create Marker at the playhead, then optionally rename it through Navigate > Rename Marker."""
    # The Navigate > Create Marker menu item is inert under AXPress (measured: three presses, still
    # "0 Markers"); the key command creates one every time. It is a single deliberate chord sent with
    # Logic frontmost, not free text.
    keys.press("alt+'", pid)
    time.sleep(0.5)
    if not name:
        return {"ok": True, "named": False}
    if not ax_logic.press_menu_item(root, "Navigate", "Rename Marker"):
        return {"ok": True, "named": False, "reason": "Rename Marker not available"}
    time.sleep(0.5)
    import ApplicationServices as AS
    err, el = AS.AXUIElementCopyAttributeValue(root, "AXFocusedUIElement", None)
    if err or el is None or ax.role(el) not in ("AXTextField", "AXTextArea") or not ax.attr(el, "AXFocused"):
        keys.press("escape", pid)
        return {"ok": True, "named": False, "reason": "rename editor never took focus; refusing to type"}
    if ax.set_value(el, name):
        ax.perform(el, "AXConfirm")
    else:
        keys.press("cmd+a", pid); keys.type_keys(name, pid); keys.press("return", pid)
    time.sleep(0.3)
    return {"ok": True, "named": True}
