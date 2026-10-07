"""Standard macOS panels and Logic alerts.

Save panels carry stable identifiers regardless of which app opened them:
  saveAsNameTextField · where popup · NewFolderButton · CancelButton · OKButton (title varies: "Save"/"Bounce")

IMPORTANT: a sandboxed app's save panel is hosted by com.apple.appkit.xpc.openAndSavePanelService, a
separate process. Its elements are proxied into the host app's AX tree, so reads and AXValue writes work,
but AXPress on its buttons silently fails and key events posted to the host app's pid go nowhere. Drive it
with HID-level input instead: keys with pid=None and real mouse clicks, after activating the owning app.
Logic alerts are AXWindow desc='alert' with action-button-1 (confirm) / action-button-2 (cancel).
"""
from __future__ import annotations

import os
import time

from . import ax, keys

SAVE_NAME = "saveAsNameTextField"
OK_BUTTON = "OKButton"
CANCEL_BUTTON = "CancelButton"


# ---------------------------------------------------------------- generic
def other_windows(root, main_suffix: str = "- Tracks") -> list:
    return [x for x in ax.windows(root) if not str(ax.title(x) or "").endswith(main_suffix)]


def wait_for(pred, timeout: float = 5.0, step: float = 0.15):
    end = time.time() + timeout
    while time.time() < end:
        v = pred()
        if v:
            return v
        time.sleep(step)
    return None


def wait_gone(pred, timeout: float = 10.0, step: float = 0.2) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        if not pred():
            return True
        time.sleep(step)
    return False


def find_by_id(root, ident: str, max_depth: int = 10):
    return ax.find(root, id_=ident, max_depth=max_depth)


def button(win, title: str, max_depth: int = 6):
    return ax.find(win, role_="AXButton", title_=title, max_depth=max_depth)


def cancel_all(root, tries: int = 3) -> list[str]:
    """Best-effort: dismiss every non-main window. Returns titles it acted on."""
    acted = []
    for _ in range(tries):
        wins = other_windows(root)
        if not wins:
            break
        for x in wins:
            t = str(ax.title(x) or "")
            b = find_by_id(x, CANCEL_BUTTON) or button(x, "Cancel")
            if b is not None and ax.press(b):
                acted.append(t)
            time.sleep(0.5)
    return acted


# ---------------------------------------------------------------- alerts
def alert(root):
    for x in ax.windows(root):
        if str(ax.desc(x) or "") == "alert":
            return x
    return None


def alert_text(win) -> list[str]:
    return [str(ax.value(t)) for t in ax.find_all(win, lambda e: ax.role(e) == "AXRole" or ax.role(e) == "AXStaticText", max_depth=3, limit=4) if ax.value(t)]


def alert_buttons(win) -> dict:
    out = {}
    for el in ax.find_all(win, lambda e: ax.role(e) == "AXButton", max_depth=3, limit=6):
        out[str(ax.title(el) or ax.ident(el))] = el
    return out


def answer_alert(root, choice: str = "Cancel", timeout: float = 2.0) -> dict | None:
    """Read an alert and press one of its buttons by title. Returns what it saw."""
    win = wait_for(lambda: alert(root), timeout)
    if win is None:
        return None
    text = alert_text(win)
    btns = alert_buttons(win)
    el = btns.get(choice)
    pressed = bool(el is not None and ax.press(el))
    return {"text": text, "buttons": list(btns), "chose": choice, "pressed": pressed}


# ---------------------------------------------------------------- save panel
def save_panel(root, timeout: float = 6.0):
    def look():
        for x in other_windows(root):
            if find_by_id(x, SAVE_NAME) is not None:
                return x
        return None
    return wait_for(look, timeout)


GOTO_FIELD = "PathTextField"


def focused_field_in(panel):
    """The text field that currently has keyboard focus, if it lives inside `panel`; else None.

    Typing into a save panel is HID-level (the panel belongs to another process), so every character goes
    to whatever really has focus. Typed blind into Logic's main window, a filename became a run of key
    commands that resized the window, opened editors, moved the playhead and created a track stack. So
    nothing is typed unless the system-wide focused element is a text field whose frame sits inside the
    panel's frame.
    """
    import ApplicationServices as AS
    from . import screen
    err, el = AS.AXUIElementCopyAttributeValue(AS.AXUIElementCreateSystemWide(), "AXFocusedUIElement", None)
    if err or el is None or ax.role(el) not in ("AXTextField", "AXComboBox", "AXTextArea"):
        return None
    fr, pr = screen.element_rect(el), screen.element_rect(panel)
    if fr is None or pr is None:
        return None
    inside = pr[0] - 2 <= fr[0] and fr[0] + fr[2] <= pr[0] + pr[2] + 2 and pr[1] - 2 <= fr[1] and fr[1] + fr[3] <= pr[1] + pr[3] + 2
    return el if inside else None


def set_directory(panel, pid: int | None, directory: str, settle: float = 1.2) -> dict:
    """Cmd+Shift+G, type the path, Return. HID-level because the panel is another process's window."""
    directory = os.path.expanduser(directory)
    if focused_field_in(panel) is None:
        # Give the panel keyboard focus by clicking its own name field (a real click: the panel belongs to
        # the open/save XPC service), then re-check rather than assume.
        from . import mouse, screen
        tf = find_by_id(panel, SAVE_NAME) or ax.find(panel, role_="AXTextField", max_depth=6)
        r = screen.element_rect(tf) if tf is not None else None
        if r is not None:
            mouse.click(r[0] + r[2] / 2, r[1] + r[3] / 2); time.sleep(0.4)
    if focused_field_in(panel) is None:
        return {"ok": False, "reason": "save panel does not have keyboard focus; refusing to send Cmd+Shift+G"}
    keys.press("cmd+shift+g", None); time.sleep(0.8)
    sheet = next((c for c in ax.children(panel) if ax.role(c) == "AXSheet"), None)
    field = focused_field_in(panel)
    if field is None:
        keys.press("escape", None)
        return {"ok": False, "reason": "Go To Folder field never took focus; refusing to type the path"}
    if not ax.set_value(field, directory) or str(ax.value(field) or "") != directory:
        keys.type_keys(directory, None); time.sleep(0.5)           # only reached with focus proven above
    typed = str(ax.value(field) or "")
    keys.press("return", None); time.sleep(settle)
    where = str(ax.value(find_by_id(panel, "where popup")) or "")
    ok = where != "" and (where == os.path.basename(directory.rstrip("/")) or typed == directory)
    return {"ok": ok, "where": where, "typed": typed, "sheet_seen": sheet is not None}


def set_filename(panel, name: str, pid: int | None = None) -> dict:
    tf = find_by_id(panel, SAVE_NAME)
    if tf is None:
        return {"ok": False, "reason": "name field not found"}
    before = str(ax.value(tf) or "")
    if ax.set_value(tf, name):
        time.sleep(0.3)
        after = str(ax.value(tf) or "")
        if after == name:
            return {"ok": True, "before": before, "after": after, "how": "set_value"}
    field = focused_field_in(panel)
    if field is None:
        return {"ok": False, "before": before, "after": str(ax.value(tf) or ""),
                "reason": "name field does not have keyboard focus; refusing to type"}
    keys.press("cmd+a", None); time.sleep(0.1)
    keys.type_keys(name, None); time.sleep(0.3)                    # only reached with focus proven above
    after = str(ax.value(tf) or "")
    return {"ok": after == name, "before": before, "after": after, "how": "typed"}


def confirm_save(panel, app_name: str | None = None) -> bool:
    """AXPress does not work on this panel; click the button for real with its app frontmost."""
    from . import mouse, screen
    b = find_by_id(panel, OK_BUTTON)
    if b is None:
        return False
    r = screen.element_rect(b)
    if r is None:
        return ax.press(b)
    from . import ax_logic
    if app_name:
        import AppKit
        ra = next((x for x in AppKit.NSWorkspace.sharedWorkspace().runningApplications() if x.localizedName() == app_name), None)
        if ra is not None:
            ax_logic.raise_app(ax.app(ra.processIdentifier()), None, ra.processIdentifier()); time.sleep(0.4)
    if screen.topmost_owner_pid(r[0] + r[2] / 2, r[1] + r[3] / 2) is None:
        return False
    return mouse.click(r[0] + r[2] / 2, r[1] + r[3] / 2, pid=None)


# ---------------------------------------------------------------- popups
def popup_value(el) -> str | None:
    return None if el is None else str(ax.value(el) or "")


def popup_options(el, root=None) -> list[str]:
    """Open a popup, read its items, close it again."""
    if el is None or not ax.press(el):
        return []
    time.sleep(0.35)
    menu = ax.find(el, role_="AXMenu", max_depth=2)
    items = [str(ax.title(k)) for k in ax.children(menu) if ax.title(k)] if menu is not None else []
    ax.perform(el, "AXCancel") or keys.press("escape", None)
    time.sleep(0.2)
    return items


def set_popup(el, value: str, pid: int | None = None, settle: float = 0.35) -> dict:
    """Press the popup, pick the item whose title matches, verify by reading the popup back."""
    if el is None:
        return {"ok": False, "reason": "popup not found"}
    before = popup_value(el)
    if before == value:
        return {"ok": True, "before": before, "after": before, "how": "noop"}
    if not ax.press(el):
        return {"ok": False, "before": before, "reason": "press failed"}
    time.sleep(settle)
    menu = ax.find(el, role_="AXMenu", max_depth=2)
    if menu is None:
        keys.press("escape", pid)
        return {"ok": False, "before": before, "reason": "menu did not open"}
    hit = None
    for k in ax.children(menu):
        t = str(ax.title(k) or "")
        if t == value or t.replace(" ", "").lower() == value.replace(" ", "").lower():
            hit = k
            break
    if hit is None:
        titles = [str(ax.title(k)) for k in ax.children(menu) if ax.title(k)]
        ax.perform(el, "AXCancel") or keys.press("escape", pid)
        return {"ok": False, "before": before, "reason": f"no option {value!r}", "options": titles}
    ax.press(hit); time.sleep(settle)
    after = popup_value(el)
    return {"ok": after == value, "before": before, "after": after, "how": "menu"}


# ---------------------------------------------------------------- segmented bar/beat fields
def segment_group(win, label_value: str, max_depth: int = 4):
    """Logic's bar/beat/div/tick fields render as an AXGroup whose value is '\\t1\\t1\\t1\\t1'.
    `label_value` picks which one by the AXStaticText that precedes it (e.g. 'Start:', 'End:')."""
    kids = ax.children(win)
    idx = None
    for i, k in enumerate(kids):
        if ax.role(k) == "AXStaticText" and str(ax.value(k) or "") == label_value:
            idx = i
            break
    if idx is None:
        return None
    for k in kids[idx + 1:idx + 3]:
        if ax.role(k) == "AXGroup" and "\t" in str(ax.value(k) or ""):
            return k
    return None


def segment_values(group) -> list[int]:
    return [int(x) for x in str(ax.value(group) or "").split("\t") if x.strip().isdigit()]


def set_segments(group, values: list[int], pid: int) -> dict:
    """Click the field, then type the numbers separated by spaces (the same trick Go To Position needs)."""
    from . import mouse, screen
    before = segment_values(group)
    r = screen.element_rect(group)
    if r is None:
        return {"ok": False, "reason": "no rect", "before": before}
    mouse.click(r[0] + 12, r[1] + r[3] / 2, pid=None); time.sleep(0.35)
    keys.type_keys(" ".join(str(v) for v in values), pid); time.sleep(0.2)
    keys.press("return", pid); time.sleep(0.4)
    after = segment_values(group)
    return {"ok": after[:len(values)] == values, "before": before, "after": after}

def popup_for_label(win, label: str, max_depth: int = 6):
    """Find the popup that belongs to a text label (e.g. 'File Type:').

    Logic reuses AXIdentifiers across panels, so match by geometry instead: the popup on the same
    row as the label, nearest to its right; falling back to the nearest one below it.
    """
    from . import screen
    lab = None
    for el in ax.find_all(win, lambda e: ax.role(e) == "AXStaticText", max_depth=max_depth, limit=60):
        if str(ax.value(el) or "") == label:
            lab = el
            break
    if lab is None:
        return None
    lr = screen.element_rect(lab)
    if lr is None:
        return None
    lcy = lr[1] + lr[3] / 2
    best, bestd = None, 1e9
    for el in ax.find_all(win, lambda e: ax.role(e) == "AXPopUpButton", max_depth=max_depth, limit=30):
        r = screen.element_rect(el)
        if r is None:
            continue
        cy = r[1] + r[3] / 2
        dy = abs(cy - lcy)
        if dy <= max(lr[3], r[3]) * 0.75 and r[0] >= lr[0] - 4:      # same row, to the right
            d = dy * 10 + (r[0] - lr[0])
        elif r[1] >= lr[1] + lr[3] - 2 and abs(r[0] - lr[0]) < 90:   # directly below
            d = 1000 + (r[1] - lr[1])
        else:
            continue
        if d < bestd:
            best, bestd = el, d
    return best


def labels_ending(win, suffix: str = ":", max_depth: int = 6) -> list[str]:
    return [str(ax.value(e)) for e in ax.find_all(win, lambda e: ax.role(e) == "AXStaticText", max_depth=max_depth, limit=60)
            if str(ax.value(e) or "").endswith(suffix)]
