"""Plugin control: insert by name, read every parameter in real units, write to a target value.

Both reference projects declared this impossible. It isn't. A plugin window exposes each control as a
named element carrying a real-unit readout:

    AXSlider desc='Peak 2 Gain' value=240.0 AXValueDescription='0.0 dB' min=0 max=480

Write mechanics, measured on Logic 12.3:
  * AXValue is settable but moves ONE internal unit per call, whatever number you pass. It is a nudge.
  * AXIncrement / AXDecrement move TEN internal units.
  * The mapping from internal units to real units is not linear (frequency is logarithmic), so the only
    honest way to hit a target is to step and re-read the readout each time.
"""
from __future__ import annotations

import math
import re
import time

from .channels import ax, ax_logic, dialogs, keys, labels, mouse, screen
from .result import fail, ok, unsure

STEP_COARSE = 10.0   # units moved by AXIncrement / AXDecrement
STEP_FINE = 1.0      # units moved by an AXValue write

UNIT_RE = re.compile(r"^\s*([+-]?\d*\.?\d+)\s*(k?)\s*([A-Za-z%°/]*)\s*$")
RATIO_RE = re.compile(r"^\s*([+-]?\d*\.?\d+)\s*:\s*([+-]?\d*\.?\d+)\s*$")   # Compressor "4:1"


def parse_value(text: str | None) -> tuple[float, str] | None:
    """'+3.1 dB' -> (3.1, 'dB') · '250 Hz' -> (250.0, 'Hz') · '1.2 kHz' -> (1200.0, 'Hz') · '0.30 ' -> (0.3, '')"""
    if text is None:
        return None
    s = str(text).strip()
    if not s:
        return None
    if "∞" in s or "inf" in s.lower():
        neg = s.lstrip().startswith("-")            # "+∞ dB" is not "-∞ dB"
        return (float("-inf") if neg else float("inf"), "dB")
    m = RATIO_RE.match(s)
    if m:
        try:
            a, b = float(m.group(1)), float(m.group(2))
            return (a / b, ":1") if b else None     # 4:1 -> 4.0
        except ValueError:
            return None
    m = UNIT_RE.match(s)
    if not m:
        return None
    num, kilo, unit = m.group(1), m.group(2), m.group(3)
    try:
        v = float(num)
    except ValueError:
        return None
    if kilo:
        v *= 1000.0
        unit = unit or "Hz"
    return (v, unit)


def parse_target(spec: str | float | int) -> tuple[float, str]:
    """Accepts 3.0, '3', '+3 dB', '250 Hz', '1.2 kHz', '18 %'."""
    if isinstance(spec, (int, float)):
        return (float(spec), "")
    p = parse_value(str(spec))
    if p is None:
        raise ValueError(f"cannot read a number out of {spec!r}")
    return p


# ---------------------------------------------------------------- windows & discovery
def is_plugin_window(win) -> bool:
    """A plugin editor always carries a bypass switch. Title alone is not enough — the editor is titled with
    the channel strip name, and the Mixer, Piano Roll and every dialog would otherwise look like one."""
    # 'bypass' is an AXCheckBox on Channel EQ and an AXButton on the Compressor, so match on the
    # description alone; pair it with the editor's close button to avoid false positives.
    has_bypass = any(labels.matches(ax.desc(c), "bypass") for c in ax.children(win))
    has_close = any(labels.matches(ax.desc(c), "close") for c in ax.children(win))
    return has_bypass and has_close


def plugin_windows(root, main_suffix: str = "- Tracks") -> list:
    """Open plugin editors, identified by structure rather than by not being the arrange window."""
    return [x for x in ax.windows(root)
            if not ax_logic.is_main_window(x) and not str(ax.title(x) or "").endswith(main_suffix) and is_plugin_window(x)]


def close_plugin_windows(root) -> list[str]:
    closed = []
    for x in plugin_windows(root):
        t = str(ax.title(x) or "")
        b = ax_logic.find_label(x, "close", role="AXButton", depth=2)
        if b is not None and ax.press(b):
            closed.append(t)
            time.sleep(0.3)
    return closed


def editor_name(win, name: str) -> bool:
    channel = str(ax.title(win) or "")
    expected = {candidate.casefold() for candidate in labels.variants(name)}
    expected.update(f"{candidate} {channel}".strip().casefold() for candidate in labels.variants(name))
    return any(ax.role(el) == "AXStaticText" and str(ax.value(el) or "").casefold() in expected
               for el in ax.children(win))


def controls_rows(win) -> list:
    """A Controls-view cell groups its own label and real-unit readout."""
    rows = []
    for cell in ax.find_all(win, lambda el: ax.role(el) == "AXCell", max_depth=8, limit=200):
        kids = ax.children(cell)
        row_labels = [str(ax.value(el) or "").strip() for el in kids if ax.role(el) == "AXStaticText"]
        labels_with_colon = [name.rstrip(":：").strip() for name in row_labels if name.endswith((":", "："))]
        if len(labels_with_colon) != 1:
            continue
        candidates = [el for el in kids if ax.role(el) in ("AXCheckBox", "AXPopUpButton")]
        for group in (el for el in kids if ax.role(el) == "AXGroup"):
            for slider in ax.children(group):
                display = ax.attr(slider, "AXValueDescription")
                if ax.role(slider) == "AXSlider" and display == ax.value(group) and parse_value(display) is not None:
                    candidates.append(slider)
        if len(candidates) == 1:
            rows.append((labels_with_colon[0], candidates[0]))
    return rows


def reveal_compressor_controls(win) -> bool:
    if controls_rows(win):
        return True
    view = ax_logic.find_label(win, "view", role="AXMenuButton", depth=2)
    if view is None or not ax.press(view):
        return False
    menu = dialogs.wait_for(lambda: ax_logic.find_label(view, "Controls", attr=ax.title, role="AXMenuItem"), 1.2)
    if menu is None or not ax.press(menu):
        ax.perform(view, "AXCancel")
        return False
    return dialogs.wait_for(lambda: controls_rows(win), 1.5) is not None


def _controls(win) -> list:
    return ax.find_all(win, lambda e: ax.role(e) in ("AXSlider", "AXCheckBox", "AXPopUpButton", "AXButton")
                       and ax.desc(e), max_depth=6, limit=400)


def _unnamed_sliders(win) -> list:
    """Plugins vary wildly in how much they label. Channel EQ names every control in AXDescription;
    the Compressor leaves its sliders anonymous and puts the names in nearby text elements."""
    out = []
    for el in ax.find_all(win, lambda e: ax.role(e) == "AXSlider" and not ax.desc(e), max_depth=8, limit=200):
        if ax.attr(el, "AXValueDescription") is None:
            continue                     # skip the inner indicator sliders, which carry no readout
        out.append(el)
    return out


def _text_labels(win) -> list[tuple[str, tuple]]:
    from .channels import screen
    out = []
    for el in ax.find_all(win, lambda e: ax.role(e) in ("AXStaticText", "AXTextField"), max_depth=8, limit=200):
        v = str(ax.value(el) or "").strip()
        if not v or len(v) > 28 or parse_value(v) is not None:
            continue                     # a numeric readout is not a label
        r = screen.element_rect(el)
        if r:
            out.append((v, r))
    return out


def label_for(win, el, labels: list | None = None) -> str | None:
    """Nearest text element to a control, preferring one directly below it (Logic's usual layout),
    then one to its left, then simply the closest."""
    from .channels import screen
    r = screen.element_rect(el)
    if r is None:
        return None
    labels = labels if labels is not None else _text_labels(win)
    cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
    best, bestd = None, 1e9
    for text, lr in labels:
        lcx, lcy = lr[0] + lr[2] / 2, lr[1] + lr[3] / 2
        dx, dy = abs(lcx - cx), lcy - cy
        if dx > max(r[2], 90):
            continue
        d = (dy if dy >= 0 else -dy * 2.5) + dx * 0.6   # below is much likelier than above
        if d < bestd:
            best, bestd = text, d
    return best


def describe(win) -> dict:
    """Every addressable control in a plugin window, with its real-unit readout."""
    params, switches, menus = [], [], []
    rows = controls_rows(win)
    candidates = [(str(ax.desc(el) or ""), el, None) for el in _controls(win)]
    if rows:
        candidates.extend((name, el, "row") for name, el in rows)
    else:
        text_labels = _text_labels(win)
        candidates.extend((label_for(win, el, text_labels), el, "position") for el in _unnamed_sliders(win))

    for name, el, labelled_by in candidates:
        if not name:
            continue
        role = ax.role(el)
        if role == "AXSlider":
            vd = ax.attr(el, "AXValueDescription")
            p = parse_value(vd)
            param = {"name": name, "display": str(vd) if vd else None,
                     "value": p[0] if p else None, "unit": p[1] if p else None,
                     "raw": ax_logic._num(ax.value(el)),
                     "raw_min": ax_logic._num(ax.attr(el, "AXMinValue")),
                     "raw_max": ax_logic._num(ax.attr(el, "AXMaxValue"))}
            if labelled_by:
                param["labelled_by"] = labelled_by
            params.append(param)
        elif role == "AXCheckBox":
            switches.append({"name": name, "on": ax_logic._truthy(ax.value(el))})
        elif role == "AXPopUpButton":
            menus.append({"name": name, "value": str(ax.value(el) or "")})
    preset = ax.find(win, role_="AXPopUpButton", max_depth=2)
    return {"window": str(ax.title(win) or ""), "preset": str(ax.value(preset) or "") if preset is not None else None,
            "parameters": params, "switches": switches, "menus": menus}


def resolve_control(win, name: str, role: str | None = None) -> tuple:
    """(element, reason). reason is 'ok', 'not_found' or 'ambiguous' — the caller should not have to guess."""
    rows = controls_rows(win)
    cands = [(str(ax.desc(el) or ""), el) for el in _controls(win)] + rows
    exact_names = {n.casefold() for n in labels.variants(name)}
    role_matches = [(lab, el) for lab, el in cands if not role or ax.role(el) == role]
    exact = [el for lab, el in role_matches if lab.casefold() in exact_names]
    if exact:
        return (exact[0], "ok") if len(exact) == 1 else (None, "ambiguous")
    if not rows and role in (None, "AXSlider"):
        text_labels = _text_labels(win)
        role_matches.extend((lab, el) for el in _unnamed_sliders(win)
                            if (lab := label_for(win, el, text_labels)))
    for pick in (lambda lab: lab.casefold() in exact_names,
                 lambda lab: any(n in lab.casefold() for n in exact_names)):
        hits = [el for lab, el in role_matches if pick(lab)]
        if hits:
            return (hits[0], "ok") if len(hits) == 1 else (None, "ambiguous")
    return None, "not_found"


def find_control(win, name: str, role: str | None = None):
    return resolve_control(win, name, role)[0]


# ---------------------------------------------------------------- writing a parameter
def _read(el) -> tuple[float | None, float | None, str]:
    """(real value, internal value, display text)"""
    vd = ax.attr(el, "AXValueDescription")
    p = parse_value(vd)
    return (p[0] if p else None, ax_logic._num(ax.value(el)), str(vd) if vd else "")


def owning_switch(win, param_name: str):
    """'Low Cut Frequency' -> the 'Low Cut' checkbox. Logic freezes a band's controls while it is off."""
    words = param_name.split()
    for n in range(len(words) - 1, 0, -1):
        el = find_control(win, " ".join(words[:n]), role="AXCheckBox")
        if el is not None:
            return el
    return None


def set_parameter(el, target: float, tolerance: float | None = None, max_moves: int = 120,
                  max_burst: int = 12) -> dict:
    """Drive a plugin slider to a target real-world value, re-reading the readout after every move.

    The internal->real mapping is non-linear (frequency is logarithmic), so the step size is measured
    locally and re-measured whenever the estimate overshoots. Every burst is bounded, because a control
    whose readout barely moves would otherwise be blasted to its limit in one go.
    """
    real, raw, disp = _read(el)
    if real is None or raw is None:
        return {"ok": False, "reason": "control has no numeric readout", "display": disp}
    if ax.attr(el, "AXEnabled") is False:
        return {"ok": False, "reason": "control is disabled — its band or section is switched off",
                "value": real, "display": disp, "fix": "turn the band on first"}
    if not math.isfinite(target):
        return {"ok": False, "reason": "target must be a finite number", "value": real, "display": disp}
    start = {"value": real, "raw": raw, "display": disp}
    if tolerance is None:
        tolerance = max(abs(target) * 0.01, 0.05)

    # A raw step counts as movement relative to the control's own scale: a normalised 0..1 slider moves in
    # hundredths, so a fixed 0.5-unit threshold would call every real move a stall.
    span = (ax_logic._num(ax.attr(el, "AXMaxValue")) or 0) - (ax_logic._num(ax.attr(el, "AXMinValue")) or 0)
    raw_eps = max(abs(span) * 1e-4, 1e-6) if span else 1e-6

    def move(units: float) -> None:
        if units >= STEP_COARSE:
            ax.perform(el, "AXIncrement")
        elif units <= -STEP_COARSE:
            ax.perform(el, "AXDecrement")
        else:
            ax.set_value(el, (ax_logic._num(ax.value(el)) or 0) + (STEP_FINE if units > 0 else -STEP_FINE))

    def settle(note: str, moves: int) -> dict:
        r, rw, d = _read(el)
        return {"ok": r is not None and abs(target - r) <= tolerance, "start": start,
                "value": r, "raw": rw, "display": d, "moves": moves, "note": note}

    moves = 0
    stalled = 0
    while moves < max_moves:
        real, raw, disp = _read(el)
        if real is None or raw is None:
            return settle("readout became unreadable", moves)
        diff = target - real
        if abs(diff) <= tolerance:
            return {"ok": True, "start": start, "value": real, "raw": raw, "display": disp, "moves": moves}

        before_real, before_raw = real, raw
        move(STEP_COARSE if diff > 0 else -STEP_COARSE)
        time.sleep(0.05)
        moves += 1
        after_real, after_raw, disp = _read(el)
        if after_real is None or after_raw is None:
            return settle("readout became unreadable", moves)

        if abs(after_raw - before_raw) < raw_eps:          # the control did not actually move
            stalled += 1
            if stalled >= 2:
                return settle("control hit its limit", moves)
            continue
        stalled = 0
        gained = after_real - before_real
        if gained == 0:
            continue

        remaining = target - after_real
        if (remaining > 0) != (gained > 0):                # overshot — close the rest one step at a time
            while moves < max_moves:
                r2, raw2, disp = _read(el)
                if r2 is None:
                    return settle("readout became unreadable", moves)
                d2 = target - r2
                if abs(d2) <= tolerance:
                    return {"ok": True, "start": start, "value": r2, "raw": raw2, "display": disp, "moves": moves}
                move(STEP_FINE if d2 > 0 else -STEP_FINE)
                time.sleep(0.05)
                moves += 1
                r3, _, _ = _read(el)
                if r3 is None or (target - r3 > 0) != (d2 > 0):
                    return settle("closest reachable step", moves)
            return settle("ran out of moves", moves)

        # Bounded burst: never more than max_burst blind steps before looking again.
        jumps = min(int(abs(remaining / gained) * 0.8), max_burst, max_moves - moves)
        for _ in range(max(jumps, 0)):
            move(STEP_COARSE if remaining > 0 else -STEP_COARSE)
            moves += 1
        time.sleep(0.08)

    return settle("ran out of moves", moves)


# ---------------------------------------------------------------- insertion
def insert(root, pid: int, app_name: str, plugin: str, timeout: float = 6.0) -> dict:
    """Find the exact search result and confirm the identity of the editor that opens."""
    before = plugin_windows(root)
    if dialogs.alert(root) is not None:
        return fail("plugins", "dismiss Logic's alert before inserting a plugin")
    if not ax_logic.raise_app(root, ax_logic.main_window(root), pid):
        return fail("plugins", "could not activate Logic's project window")
    if not ax_logic.press_menu_item(root, "Mix", "~^(?:Search and Add Plug-in|搜索并添加插件)"):
        return fail("menu", "Mix > Search and Add Plug-in… not found")

    def search_dialog():
        for win in dialogs.other_windows(root):
            if is_plugin_window(win):
                continue
            for field in ax.find_all(win, lambda el: ax.role(el) == "AXTextField", max_depth=4, limit=4):
                placeholder = str(ax.attr(field, "AXPlaceholderValue") or "")
                if re.search(r"Search.*Plug-in|搜索.*插件", placeholder, re.I):
                    return win, field
            field = ax.find(win, role_="AXTextField", max_depth=4)
            if not ax.title(win) and field is not None and ax.find(win, role_="AXOutline", max_depth=5) is not None:
                return win, field
        return None

    match = dialogs.wait_for(search_dialog, 4.0)
    if match is None:
        return fail("plugins", "search dialog did not open", {"windows": ax_logic.window_titles(root)})
    dlg, field = match

    def rows():
        results = []
        for row in ax.find_all(dlg, lambda e: ax.role(e) == "AXRow", max_depth=7, limit=30):
            label = ax.find(row, role_="AXStaticText", max_depth=3)
            text = (ax.value(label) if label is not None else None) or ax.title(row) or ax.desc(row)
            if text:
                results.append((str(text), row))
        return results

    def query(text: str):
        # HID-level typing lets the SwiftUI search field process spaces and focus normally.
        if not ax.attr(field, "AXFocused"):
            return []
        if ax.value(field):
            keys.press("cmd+a", pid)
            time.sleep(0.09)
        keys.type_text(text, pid)
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline:
            time.sleep(0.15)
            found = rows()
            if str(ax.value(field) or "") == text and found:
                return found
        return []

    wanted = labels.variants(plugin)
    found = query(plugin)
    exact = [(name, row) for name, row in found if name.casefold() in {n.casefold() for n in wanted}]
    if not exact and len(wanted) > 1:
        found = query(wanted[1])
        exact = [(name, row) for name, row in found if name.casefold() == wanted[1].casefold()]
    if not exact and " " in plugin:
        found = query(plugin.split()[0])
        exact = [(name, row) for name, row in found if name.casefold() in {n.casefold() for n in wanted}]
    if len(exact) != 1:
        if ax.attr(field, "AXFocused"):
            keys.press("escape")
        return fail("plugins", f"no unique match for {plugin!r}", {"matches": [name for name, _ in found]})

    name, row = exact[0]
    rect = screen.element_rect(row)
    if rect is None:
        return fail("plugins", f"search result {name!r} is not on screen")
    x, y = rect[0] + rect[2] / 2, rect[1] + rect[3] / 2
    if not ax_logic.owns_point(pid, x, y):
        return fail("plugins", f"search result {name!r} is covered by another window")
    mouse.click(x, y)
    time.sleep(0.35)
    if search_dialog() is not None and ax_logic.owns_point(pid, x, y):
        keys.press("return")

    def opened():
        for win in plugin_windows(root):
            if win in before:
                continue
            if editor_name(win, plugin):
                return win
        return None

    win = dialogs.wait_for(opened, timeout)
    if win is None:
        return unsure("plugins", "the requested plugin editor could not be verified", {"matched": name})
    return ok("plugins", f"inserted {name}", {"window": str(ax.title(win) or ""), "matched": name})
