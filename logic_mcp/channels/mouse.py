"""Mouse channel: real clicks for controls whose AXPress is inert (Logic's colour swatches, track headers).

Two delivery modes:
  pid  — CGEventPostToPid: no focus change, cursor stays put. Works for many AppKit views.
  hid  — CGEventPost to the HID tap: moves the real cursor (restored afterwards); target app must be frontmost.

KNOWN FAILURE MODE — a stale Accessibility grant silently kills both.
CGEventPost degrades instead of erroring when the *calling* process' Accessibility grant goes stale:
mouse-moved events still move the cursor, but button and key events are dropped. Nothing reports an
error, AXIsProcessTrusted() still answers True, and the events are still visible at the HID, session and
annotated-session taps — they simply never reach the target app. Toggling the permission in System
Settings does not refresh an already-running process, so a long-lived host keeps posting dead events for
the rest of its life; only relaunching the process that posts them restores clicking.
Use `posting_alive()` to detect this before trusting a click, and say so rather than reporting success.
"""
from __future__ import annotations

import subprocess
import time

import Quartz as Q

MODS = {"cmd": Q.kCGEventFlagMaskCommand, "shift": Q.kCGEventFlagMaskShift,
        "alt": Q.kCGEventFlagMaskAlternate, "opt": Q.kCGEventFlagMaskAlternate,
        "ctrl": Q.kCGEventFlagMaskControl}


MODIFIER_KEYCODES = (55, 54, 56, 60, 58, 61, 59, 62)   # cmd/shift/opt/ctrl, both sides


def clear_modifiers(settle: float = 0.12) -> int:
    """Force every modifier back up and return the residual flag state.

    A modified click sets flags on its own mouse events, which leaves the session's modifier state
    latched: macOS keeps reporting Command as held, so the next *plain* click is silently a cmd-click.
    Measured: after one cmd-click, clicking a track header added it to the selection instead of
    replacing it, which would have built track stacks out of the wrong tracks.
    """
    for code in MODIFIER_KEYCODES:
        up = Q.CGEventCreateKeyboardEvent(None, code, False)
        if up is not None:
            Q.CGEventSetFlags(up, 0)
            Q.CGEventPost(Q.kCGHIDEventTap, up)
    fc = Q.CGEventCreateKeyboardEvent(None, 0, False)
    if fc is not None:
        Q.CGEventSetType(fc, Q.kCGEventFlagsChanged)
        Q.CGEventSetFlags(fc, 0)
        Q.CGEventPost(Q.kCGHIDEventTap, fc)
    time.sleep(settle)
    return Q.CGEventSourceFlagsState(Q.kCGEventSourceStateCombinedSessionState)


def _ev(kind, x: float, y: float):
    return Q.CGEventCreateMouseEvent(None, kind, Q.CGPointMake(x, y), Q.kCGMouseButtonLeft)


def click(x: float, y: float, pid: int | None = None, hold: float = 0.05, restore: bool = True,
          modifiers: str | None = None) -> bool:
    """modifiers like "cmd" or "cmd+shift" — needed for adding to a selection rather than replacing it.

    Returns whether the events were *posted*, which is not the same as delivered: see the module note.
    """
    prev = Q.CGEventGetLocation(Q.CGEventCreate(None))
    move, down, up = _ev(Q.kCGEventMouseMoved, x, y), _ev(Q.kCGEventLeftMouseDown, x, y), _ev(Q.kCGEventLeftMouseUp, x, y)
    if any(e is None for e in (move, down, up)):
        return False
    flags = 0
    for m in (modifiers or "").split("+"):
        m = m.strip().lower()
        if m in MODS:
            flags |= MODS[m]
    if flags:
        Q.CGEventSetFlags(down, flags)
        Q.CGEventSetFlags(up, flags)
    Q.CGEventSetIntegerValueField(down, Q.kCGMouseEventClickState, 1)
    Q.CGEventSetIntegerValueField(up, Q.kCGMouseEventClickState, 1)
    if pid:
        Q.CGEventPostToPid(pid, move); time.sleep(0.02)
        Q.CGEventPostToPid(pid, down); time.sleep(hold)
        Q.CGEventPostToPid(pid, up)
        if flags:
            clear_modifiers()
        return True
    Q.CGEventPost(Q.kCGHIDEventTap, move); time.sleep(0.03)
    Q.CGEventPost(Q.kCGHIDEventTap, down); time.sleep(hold)
    Q.CGEventPost(Q.kCGHIDEventTap, up)
    if flags:
        clear_modifiers()          # else the modifier stays latched and poisons the next plain click
    if restore:
        time.sleep(0.03)
        Q.CGWarpMouseCursorPosition(prev)
    return True


def drag(x0: float, y0: float, x1: float, y1: float, steps: int = 14, hold: float = 0.15,
         step_delay: float = 0.025, settle: float = 0.2, restore: bool = True) -> bool:
    """A real HID drag: move, press, pause, walk the pointer to the target in small steps, release.

    Two details matter for AppKit drag sources like Logic's track headers. The pause after mouse-down
    lets the view decide this is a drag and not a click (a click on a header only selects; a drag on a
    folder stack's handle reorders it — see click_select_track). And the walk must be a run of
    LeftMouseDragged events: a single jump to the destination never crosses the drag threshold the way a
    hand does, and is ignored.

    Returns whether the events were *posted* — delivery is a separate question (module note). Callers
    must read the result back out of the app; this reorders someone's arrangement if it lands.
    """
    prev = Q.CGEventGetLocation(Q.CGEventCreate(None))
    move = _ev(Q.kCGEventMouseMoved, x0, y0)
    down = _ev(Q.kCGEventLeftMouseDown, x0, y0)
    if move is None or down is None:
        return False
    Q.CGEventSetIntegerValueField(down, Q.kCGMouseEventClickState, 1)
    Q.CGEventPost(Q.kCGHIDEventTap, move); time.sleep(0.08)
    Q.CGEventPost(Q.kCGHIDEventTap, down); time.sleep(hold)
    for i in range(1, steps + 1):
        t = i / steps
        dragged = _ev(Q.kCGEventLeftMouseDragged, x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        if dragged is None:
            return False
        Q.CGEventPost(Q.kCGHIDEventTap, dragged); time.sleep(step_delay)
    time.sleep(settle)                     # let the drop indicator land where the pointer is
    up = _ev(Q.kCGEventLeftMouseUp, x1, y1)
    if up is None:
        return False
    Q.CGEventPost(Q.kCGHIDEventTap, up)
    if restore:
        time.sleep(0.05)
        Q.CGWarpMouseCursorPosition(prev)
    return True


def posting_alive(probe, toggle_point: tuple[float, float], settle: float = 0.7) -> bool:
    """True when this process can still deliver button events.

    `probe` is a zero-arg callable returning some observable state; `toggle_point` must be a control that
    flips that state and is safe to flip twice (Logic's Cycle button qualifies). Restores what it changed.
    """
    before = probe()
    click(*toggle_point)
    time.sleep(settle)
    if probe() == before:
        return False
    click(*toggle_point)
    time.sleep(settle)
    return True


def ax_press_at(x: float, y: float, process_name: str | None = None, timeout: float = 10.0) -> bool:
    """System Events 'click at' — NOT a mouse click.

    It hit-tests the element under the point and performs AXPress on it, so it reaches controls even when
    CGEvent posting is dead, but it inherits every AXPress limitation: on Logic's track headers it is
    inert, and it will happily press whatever else sits under the point (measured: a track-zoom splitter).
    Only use it on targets whose AXPress is known to work.
    """
    script = f'tell application "System Events" to click at {{{int(round(x))}, {int(round(y))}}}'
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False
    return p.returncode == 0


def double_click(x: float, y: float, hold: float = 0.05, settle: float = 0.06) -> bool:
    """A real double-click: the second press must carry clickState 2, not two separate single clicks.

    Some Logic fields only enter edit mode on a genuine double-click — Logic's group Name cells focus
    the window and ignore a single click, so a rename there silently does nothing.
    """
    move = _ev(Q.kCGEventMouseMoved, x, y)
    if move is None:
        return False
    Q.CGEventPost(Q.kCGHIDEventTap, move)
    time.sleep(0.08)
    for state in (1, 2):
        down, up = _ev(Q.kCGEventLeftMouseDown, x, y), _ev(Q.kCGEventLeftMouseUp, x, y)
        if down is None or up is None:
            return False
        Q.CGEventSetIntegerValueField(down, Q.kCGMouseEventClickState, state)
        Q.CGEventSetIntegerValueField(up, Q.kCGMouseEventClickState, state)
        Q.CGEventPost(Q.kCGHIDEventTap, down); time.sleep(hold)
        Q.CGEventPost(Q.kCGHIDEventTap, up); time.sleep(settle)
    return True
