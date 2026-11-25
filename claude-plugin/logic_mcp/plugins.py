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

from .channels import ax, ax_logic, applescript, dialogs, keys
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
    has_bypass = any(str(ax.desc(c) or "") == "bypass" for c in ax.children(win))
    has_close = any(str(ax.desc(c) or "") == "close" for c in ax.children(win))
    return has_bypass and has_close


def plugin_windows(root, main_suffix: str = "- Tracks") -> list:
    """Open plugin editors, identified by structure rather than by not being the arrange window."""
    return [x for x in ax.windows(root)
            if not str(ax.title(x) or "").endswith(main_suffix) and is_plugin_window(x)]


def close_plugin_windows(root) -> list[str]:
    closed = []
    for x in plugin_windows(root):
        t = str(ax.title(x) or "")
        b = ax.find(x, role_="AXButton", desc_="close", max_depth=2)
        if b is not None and ax.press(b):
            closed.append(t)
            time.sleep(0.3)
    return closed


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
    for el in _controls(win):
        name = str(ax.desc(el) or "")
        role = ax.role(el)
        if role == "AXSlider":
            vd = ax.attr(el, "AXValueDescription")
            p = parse_value(vd)
            params.append({"name": name, "display": str(vd) if vd else None,
                           "value": p[0] if p else None, "unit": p[1] if p else None,
                           "raw": ax_logic._num(ax.value(el)),
                           "raw_min": ax_logic._num(ax.attr(el, "AXMinValue")),
                           "raw_max": ax_logic._num(ax.attr(el, "AXMaxValue"))})
        elif role == "AXCheckBox":
            switches.append({"name": name, "on": ax_logic._truthy(ax.value(el))})
        elif role == "AXPopUpButton":
            menus.append({"name": name, "value": str(ax.value(el) or "")})
    labels = _text_labels(win)
    for el in _unnamed_sliders(win):
        vd = ax.attr(el, "AXValueDescription")
        p = parse_value(vd)
        name = label_for(win, el, labels)
        if not name:
            continue
        params.append({"name": name, "display": str(vd) if vd else None,
                       "value": p[0] if p else None, "unit": p[1] if p else None,
                       "raw": ax_logic._num(ax.value(el)),
                       "raw_min": ax_logic._num(ax.attr(el, "AXMinValue")),
                       "raw_max": ax_logic._num(ax.attr(el, "AXMaxValue")),
                       "labelled_by": "position"})
    preset = ax.find(win, role_="AXPopUpButton", max_depth=2)
    return {"window": str(ax.title(win) or ""), "preset": str(ax.value(preset) or "") if preset is not None else None,
            "parameters": params, "switches": switches, "menus": menus}


def resolve_control(win, name: str, role: str | None = None) -> tuple:
    """(element, reason). reason is 'ok', 'not_found' or 'ambiguous' — the caller should not have to guess."""
    cands = [el for el in _controls(win) if not role or ax.role(el) == role]
    exact = [el for el in cands if str(ax.desc(el) or "") == name]
    if len(exact) == 1:
        return exact[0], "ok"
    if len(exact) > 1:
        return None, "ambiguous"
    hits = [el for el in cands if name.lower() in str(ax.desc(el) or "").lower()]
    if len(hits) == 1:
        return hits[0], "ok"
    if len(hits) > 1:
        return None, "ambiguous"
    if role in (None, "AXSlider"):
        labels = _text_labels(win)
        by_label = [(el, label_for(win, el, labels)) for el in _unnamed_sliders(win)]
        for pick in (lambda lab: lab.strip().lower() == name.lower(),
                     lambda lab: name.lower() in lab.strip().lower()):
            got = [el for el, lab in by_label if lab and pick(lab)]
            if len(got) == 1:
                return got[0], "ok"
            if len(got) > 1:
                return None, "ambiguous"
    return None, "not_found"


def find_control(win, name: str, role: str | None = None):
    """Exact AXDescription match, then a unique substring, then — for plugins that leave their sliders
    anonymous — the slider whose nearest text label matches."""
    cands = [el for el in _controls(win) if not role or ax.role(el) == role]
    exact = [el for el in cands if str(ax.desc(el) or "") == name]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return None          # e.g. Channel EQ has two sliders both called "Gain" — refuse, never guess
    hits = [el for el in cands if name.lower() in str(ax.desc(el) or "").lower()]
    if len(hits) == 1:
        return hits[0]
    if role in (None, "AXSlider"):
        labels = _text_labels(win)
        by_label = [(el, label_for(win, el, labels)) for el in _unnamed_sliders(win)]
        exact = [el for el, lab in by_label if lab and lab.strip().lower() == name.lower()]
        if len(exact) == 1:
            return exact[0]
        loose = [el for el, lab in by_label if lab and name.lower() in lab.strip().lower()]
        if len(loose) == 1:
            return loose[0]
    return None


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
    """Mix > Search and Add Plug-in… — the only reliable route, since the channel-strip slot popup
    is a custom-drawn layer with no Accessibility tree."""
    before = {str(ax.title(x) or "") for x in plugin_windows(root)}
    applescript.activate(app_name); time.sleep(0.3)
    if not ax_logic.press_menu_item(root, "Mix", "~^Search and Add Plug-in"):
        return fail("menu", "Mix > Search and Add Plug-in… not found")

    def search_dialog():
        # the search sheet is not a plugin editor: it has a text field and a results outline, no bypass
        for x in ax.windows(root):
            if str(ax.title(x) or "").endswith("- Tracks") or is_plugin_window(x):
                continue
            if ax.find(x, role_="AXTextField", max_depth=4) is not None and ax.find(x, role_="AXOutline", max_depth=5) is not None:
                return x
        return None

    dlg = dialogs.wait_for(search_dialog, 4.0)
    if dlg is None:
        return fail("plugins", "search dialog did not open", {"windows": ax_logic.window_titles(root)})
    def _rows():
        out = []
        for r in ax.find_all(dlg, lambda e: ax.role(e) == "AXRow", max_depth=6, limit=12):
            t = [str(ax.value(x)) for x in ax.find_all(r, lambda e: ax.role(e) == "AXStaticText", max_depth=3, limit=1)]
            if t:
                out.append((t[0], r))
        return out

    def _type(q: str):
        keys.press("cmd+a", pid); keys.type_keys(q, pid); time.sleep(0.9)
        return _rows()

    found = _type(plugin)
    if not found and " " in plugin:
        # Logic's plug-in search does not match a trailing qualifier the way it is displayed:
        # "DeEsser 2" returns nothing while "DeEsser" lists it. Retry on the leading word.
        found = _type(plugin.split()[0])
    names = [n for n, _ in found]
    if not names:
        keys.press("escape", pid)
        return fail("plugins", f"no plugin matches {plugin!r}")
    exact = [(n, r) for n, r in found if n.lower() == plugin.lower()]
    if not exact and len(names) > 1:
        keys.press("escape", pid)
        return fail("plugins", f"{plugin!r} is ambiguous", {"matches": names[:10]})
    if exact:
        # Return activates the HIGHLIGHTED row, which is the first result, not necessarily the one
        # asked for -- select the exact row first or "DeEsser 2" silently inserts "DeEsser (m)".
        import ApplicationServices as AS
        AS.AXUIElementSetAttributeValue(exact[0][1], "AXSelected", True)
        time.sleep(0.4)
    keys.press("return", pid); time.sleep(1.8)
    win = dialogs.wait_for(lambda: next((x for x in plugin_windows(root) if str(ax.title(x) or "") not in before), None), timeout)
    if win is None:
        return unsure("plugins", "inserted, but no editor window appeared", {"matched": names[:5]})
    return ok("plugins", f"inserted {exact[0][0] if exact else names[0]}", {"window": str(ax.title(win) or ""), "matched": names[:5]})
