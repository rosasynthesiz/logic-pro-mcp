"""Multi-track selection, track stacks and mixer groups.

Everything here rests on one awkward fact: in a long session most track headers sit outside the viewport,
where clicks land nowhere and writing AXSelected reports success while doing nothing. So every selection
scrolls the row into view, clicks it for real, and then reads the selection back before going further.
"""
from __future__ import annotations

import time

from .channels import ax, ax_logic, dialogs, mouse, screen
from .result import fail, ok, unsure

STACK_KINDS = {"folder": "folder stack", "summing": "summing stack"}   # matched case-insensitively


# ---------------------------------------------------------------- selection
def select_one(win, index: int, tries: int = 3) -> bool:
    """Selection is retried: a row can be mid-scroll when the click lands, which silently selects nothing."""
    for n in range(tries):
        if ax_logic.click_select_track(win, index):
            return True
        time.sleep(0.25 * (n + 1))
    return ax_logic.selected_indices(win) == [index]


def add_to_selection(win, index: int) -> bool:
    """Cmd-click a row so it joins the selection instead of replacing it."""
    for n in range(3):
        if not ax_logic.scroll_track_into_view(win, index):
            time.sleep(0.2); continue
        item = ax_logic.find_track(win, index)
        pt = ax_logic.header_point(item) if item is not None else None
        if pt is None:
            time.sleep(0.2); continue
        mouse.click(*pt, pid=None, modifiers="cmd")
        time.sleep(0.3)
        if index in ax_logic.selected_indices(win):
            return True
        time.sleep(0.2 * (n + 1))
    return index in ax_logic.selected_indices(win)


def select_many(win, indices: list[int], verify: bool = True) -> dict:
    """Select exactly `indices`. Returns what was actually selected — never assume it matched."""
    if not indices:
        return {"ok": False, "reason": "no tracks given"}
    wanted = sorted(set(indices))
    if not select_one(win, wanted[0]):
        return {"ok": False, "reason": f"could not select track {wanted[0]}"}
    missed = []
    for i in wanted[1:]:
        if not add_to_selection(win, i):
            missed.append(i)
    for i in list(missed):                       # one more pass for rows that lost the race
        if add_to_selection(win, i):
            missed.remove(i)
    got = ax_logic.selected_indices(win)
    result = {"ok": got == wanted, "wanted": wanted, "selected": got, "missed": missed}
    if verify and not result["ok"]:
        result["reason"] = "selection does not match what was asked for"
    return result


# ---------------------------------------------------------------- stacks
def create_stack(root, win, pid: int, app_name: str, indices: list[int], kind: str = "summing",
                 name: str | None = None) -> dict:
    """Track > Create Track Stack… on an exact set of tracks."""
    if kind not in STACK_KINDS:
        return fail("stacks", f"kind must be one of {sorted(STACK_KINDS)}")
    if len(indices) < 2:
        return fail("stacks", "a stack needs at least two tracks")
    before = len(ax_logic.list_tracks(win))   # list_tracks dedupes; track_items double-counts rows

    ax_logic.raise_app(root, win, pid); time.sleep(0.2)
    sel = select_many(win, indices)
    if not sel["ok"]:
        return fail("stacks", sel.get("reason", "selection failed"), sel)

    if not ax_logic.press_menu_item(root, "Track", "~^Create Track Stack"):
        return fail("menu", "Track > Create Track Stack… not found")
    sheet = dialogs.wait_for(lambda: next((c for c in ax.children(win) if ax.role(c) == "AXSheet"), None), 3.0) \
        or dialogs.wait_for(lambda: next((x for x in dialogs.other_windows(root)), None), 2.0)
    if sheet is None:
        return unsure("stacks", "stack dialog did not appear", {"windows": ax_logic.window_titles(root)})

    picked = False
    for el in ax.find_all(sheet, lambda e: ax.role(e) in ("AXRadioButton", "AXButton", "AXCheckBox"), max_depth=6, limit=20):
        if str(ax.title(el) or "").strip().lower() == STACK_KINDS[kind]:
            ax.press(el); time.sleep(0.3)      # AXPress works on this sheet; a click can miss the radio
            picked = True
            break
    if not picked:
        opts = [str(ax.title(e)) for e in ax.find_all(sheet, lambda e: ax.role(e) in ("AXRadioButton", "AXButton"), max_depth=6, limit=20)]
        dialogs.cancel_all(root)
        return fail("stacks", f"could not find the {STACK_KINDS[kind]!r} option", {"options": opts})

    okb = dialogs.button(sheet, "Create", max_depth=6) or dialogs.button(sheet, "OK", max_depth=6)
    if okb is None:
        dialogs.cancel_all(root)
        return fail("stacks", "no Create button in the stack dialog")
    ax.press(okb)
    time.sleep(1.8)

    after = ax_logic.list_tracks(win)
    made = len(after) - before
    if True:                            # a folder stack hides its children, so the row count DROPS
        parent = next((t for t in after if t["is_folder"] and t["selected"]), None) \
            or next((t for t in after if t["is_folder"]), None)
        detail = {"tracks_before": before, "tracks_after": len(after),
                  "parent": parent["name"] if parent else None, "parent_index": parent["index"] if parent else None}
        if parent is None:
            return unsure("stacks", "no stack parent found after Create", detail)
        if name:
            rn = _rename_track(root, win, pid, app_name, parent["index"], name)
            detail["renamed"] = rn
        return ok("stacks", f"created a {kind} stack from {len(indices)} tracks", detail)
    return unsure("stacks", "track count did not change", {"before": before, "after": len(after)})


def _rename_track(root, win, pid: int, app_name: str, index: int, new_name: str) -> dict:
    return ax_logic.rename_track_safe(root, win, index, new_name, pid)


# ---------------------------------------------------------------- groups
def create_group(root, win, pid: int, app_name: str, indices: list[int], name: str | None = None) -> dict:
    """Mix > Create Group over an exact set of tracks, then name it in the Group Settings window."""
    if len(indices) < 2:
        return fail("groups", "a group needs at least two tracks")
    ax_logic.raise_app(root, win, pid); time.sleep(0.2)
    sel = select_many(win, indices)
    if not sel["ok"]:
        return fail("groups", sel.get("reason", "selection failed"), sel)

    before = ax_logic.window_titles(root)
    if not ax_logic.press_menu_item(root, "Mix", "~^Create Group"):
        return fail("menu", "Mix > Create Group not found")
    time.sleep(1.2)
    detail = {"windows_before": before, "windows_after": ax_logic.window_titles(root), "tracks": indices}

    settings = next((x for x in dialogs.other_windows(root) if "group" in str(ax.title(x) or "").lower()), None)
    if settings is not None and name:
        tf = ax.find(settings, role_="AXTextField", max_depth=6)
        if tf is not None and ax.set_value(tf, name):
            time.sleep(0.3)
            detail["named"] = str(ax.value(tf) or "")
    if settings is not None:
        detail["settings_window"] = str(ax.title(settings) or "")
    return ok("groups", f"created a group from {len(indices)} tracks", detail)
