"""Phase 5: trimming a region by hand.

This is the thing the gesture layer can do that no other control surface can. A fader is a number, and
a number is easy to send from anywhere; a region boundary is a place on the screen, and a hand is
genuinely better at places than a mouse-and-keyboard round trip is.

It works because two pieces already existed. `ax_logic.list_regions` reads every region's real screen
rectangle out of Logic's accessibility tree, and `channels/mouse.drag` performs a true HID drag with the
press-pause AppKit needs before it will treat a press as a drag at all. What is added here is the live
form of that drag — press, then follow the hand for as long as the clutch is held, then release — plus
the guard that makes it safe to attempt.

**The guard matters more than the feature.** Grabbing a few pixels inside the edge instead of on it
moves the whole region rather than trimming it, and on a busy arrangement that is a real mess to undo by
hand. So every trim records BOTH edges first and checks afterwards that the edge we did not touch has
not moved. If it did, we dragged the body: the result is reported `failed` and, with `auto_undo`, taken
back with a Cmd-Z.
"""
from __future__ import annotations

import time

import Quartz as Q

from ..channels import ax, ax_logic, keys
from .actions import Done

# How close to the boundary the pointer must be for Logic to show the trim cursor rather than the move
# cursor. Measured by eye on 12.3 at default zoom; it is a guess that wants checking at other zooms.
EDGE_GRAB_PX = 4.0

# A region thinner than this cannot be grabbed reliably — both edge zones overlap.
MIN_REGION_PX = 14.0

# Refuse a single gesture that would change an edge by more than this. A hand jumping the width of the
# screen is a tracking glitch, not an edit.
MAX_TRIM_PX = 1400.0


def _rect(r: dict) -> tuple[float, float, float, float] | None:
    s = r.get("screen")
    if not s:
        return None
    return s["x"], s["y"], s["w"], s["h"]


def find_region(win, track: int | None = None, name: str | None = None,
                selected_only: bool = False) -> dict | None:
    """One region, or None. Ambiguity refuses rather than picking — same rule as everywhere else here."""
    rows = [r for r in ax_logic.list_regions(win, track=track) if r.get("visible")]
    if selected_only:
        rows = [r for r in rows if r.get("selected")]
    if name:
        want = name.strip().lower()
        exact = [r for r in rows if str(r["name"]).strip().lower() == want]
        rows = exact or [r for r in rows if want in str(r["name"]).lower()]
    return rows[0] if len(rows) == 1 else None


def edge_point(region: dict, edge: str) -> tuple[float, float] | None:
    """Where to put the pointer to grab an edge. Vertically centred: the top few pixels of a region are
    the loop/fade handles, and grabbing those does something else entirely."""
    rect = _rect(region)
    if rect is None:
        return None
    x, y, w, h = rect
    if w < MIN_REGION_PX:
        return None
    cy = y + h / 2.0
    return (x + 1.0, cy) if edge == "left" else (x + w - 1.0, cy)


class LiveDrag:
    """A mouse drag that stays down while the hand keeps moving.

    `mouse.drag()` is atomic — it walks from A to B and releases — which is right for a known
    destination but wrong for a hand, where the destination is wherever you stop. The clutch maps onto
    this directly: closing it presses, moving your hand moves the pointer, opening it releases.

    The press-then-pause before the first move is the same detail `mouse.drag` documents: without it
    AppKit reads the press as a click and the drag never starts.
    """

    def __init__(self, x: float, y: float, hold: float = 0.15, restore: bool = True):
        self.start = (x, y)
        self.pos = (x, y)
        self.restore = restore
        self._prev = Q.CGEventGetLocation(Q.CGEventCreate(None))
        self.down_ok = False
        self._post(Q.kCGEventMouseMoved, x, y)
        time.sleep(0.08)
        ev = self._make(Q.kCGEventLeftMouseDown, x, y)
        if ev is not None:
            Q.CGEventSetIntegerValueField(ev, Q.kCGMouseEventClickState, 1)
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            self.down_ok = True
            time.sleep(hold)

    def _make(self, kind, x, y):
        return Q.CGEventCreateMouseEvent(None, kind, (x, y), Q.kCGMouseButtonLeft)

    def _post(self, kind, x, y) -> bool:
        ev = self._make(kind, x, y)
        if ev is None:
            return False
        Q.CGEventPost(Q.kCGHIDEventTap, ev)
        return True

    def move(self, x: float, y: float | None = None) -> bool:
        """Follow the hand. Called at frame rate, so it posts one event and returns."""
        if not self.down_ok:
            return False
        y = self.pos[1] if y is None else y
        self.pos = (x, y)
        return self._post(Q.kCGEventLeftMouseDragged, x, y)

    def up(self, settle: float = 0.18) -> bool:
        if not self.down_ok:
            return False
        time.sleep(settle)                       # let Logic's drop indicator settle where the pointer is
        ok = self._post(Q.kCGEventLeftMouseUp, *self.pos)
        self.down_ok = False
        if self.restore:
            time.sleep(0.05)
            Q.CGWarpMouseCursorPosition(self._prev)
        return ok

    @property
    def dx(self) -> float:
        return self.pos[0] - self.start[0]


def trim(win, region: dict, edge: str, to_x: float, *, auto_undo: bool = True,
         logic_pid: int | None = None, tolerance: float = 3.0) -> Done:
    """Drag one edge of `region` to screen x `to_x`, then prove that is what happened.

    Returns `confirmed` only when the grabbed edge moved and the other edge did not.
    """
    if edge not in ("left", "right"):
        return Done("failed", "trim", f"edge must be left or right, not {edge!r}")
    before = _rect(region)
    if before is None:
        return Done("failed", "trim", f"'{region.get('name')}' is scrolled out of view; "
                                      "no screen rectangle to grab")
    bx, by, bw, bh = before
    if bw < MIN_REGION_PX:
        return Done("failed", "trim", f"region is only {bw:.0f} px wide; zoom in before trimming")
    grab = edge_point(region, edge)
    if grab is None:
        return Done("failed", "trim", "no grab point")
    # AX reports the region's full extent, not the clipped one: a region that runs past the right of the
    # canvas has an edge x beyond the display, and a press there lands on nothing (measured: 2013 on a
    # 1728-pt display, "nothing moved"). Refuse up front instead of reporting an uncertain miss.
    disp = Q.CGDisplayBounds(Q.CGMainDisplayID())
    if not (disp.origin.x <= grab[0] < disp.origin.x + disp.size.width):
        return Done("failed", "trim", f"the {edge} edge is off-screen (x={grab[0]:.0f}); "
                                      "scroll or zoom so the edge is visible")
    delta = to_x - grab[0]
    if abs(delta) > MAX_TRIM_PX:
        return Done("failed", "trim", f"{delta:.0f} px is not a hand movement; refusing")
    new_w = bw + (delta if edge == "right" else -delta)
    if new_w < MIN_REGION_PX:
        return Done("failed", "trim", "that would collapse the region")

    d = LiveDrag(*grab)
    if not d.down_ok:
        return Done("failed", "trim", "could not post the mouse-down")
    steps = max(6, min(24, int(abs(delta) / 12)))
    for i in range(1, steps + 1):
        d.move(grab[0] + delta * (i / steps))
        time.sleep(0.02)
    d.up()

    after = find_region(win, track=region.get("track"), name=region.get("name"))
    rect = _rect(after) if after else None
    if rect is None:
        return Done("uncertain", "trim", "cannot re-read the region to verify")
    ax_, ay, aw, ah = rect
    moved_edge = (ax_ + aw) - (bx + bw) if edge == "right" else ax_ - bx
    other_edge = ax_ - bx if edge == "right" else (ax_ + aw) - (bx + bw)

    if abs(other_edge) > tolerance:
        # We dragged the body, not the boundary. This is the failure the guard exists for.
        if auto_undo:
            keys.press("cmd+z", pid=logic_pid)
            time.sleep(0.25)
        return Done("failed", "trim",
                    f"the {'left' if edge == 'right' else 'right'} edge moved {other_edge:+.0f} px too — "
                    f"that was a move, not a trim"
                    f"{'; undone' if auto_undo else '; UNDO THIS YOURSELF'}")
    if abs(moved_edge) < tolerance:
        return Done("uncertain", "trim", "nothing moved; the grab probably missed the edge")
    if (moved_edge > 0) != (delta > 0):
        # Seen live: a -30 px request came back as +96 px (one bar) — Logic snapped or looped rather than
        # trimmed. Snap can change the magnitude, never the direction, so this is wrong and goes back.
        if auto_undo:
            keys.press("cmd+z", pid=logic_pid)
            time.sleep(0.25)
        return Done("failed", "trim",
                    f"asked for {delta:+.0f} px, the {edge} edge moved {moved_edge:+.0f} px the other way"
                    f"{'; undone' if auto_undo else '; UNDO THIS YOURSELF'}")
    return Done("confirmed", "trim", f"{edge} edge {moved_edge:+.0f} px "
                                     f"({bw:.0f} -> {aw:.0f} px wide)", value=aw)


class HandTrim:
    """Bridges the clutch onto a `LiveDrag` so the hand can trim continuously.

    Held open deliberately: `begin` on clutch close, `update` per frame, `finish` on release. The
    verification in `finish` is the same one `trim` does, because an interactive drag can miss the edge
    exactly as easily as a scripted one.
    """

    def __init__(self, win, region: dict, edge: str, logic_pid: int | None = None):
        self.win, self.region, self.edge, self.pid = win, region, edge, logic_pid
        self.before = _rect(region)
        self.drag: LiveDrag | None = None

    def begin(self) -> Done:
        if self.before is None:
            return Done("failed", "trim", "region has no screen rectangle")
        grab = edge_point(self.region, self.edge)
        if grab is None:
            return Done("failed", "trim", "region too narrow to grab an edge")
        self.drag = LiveDrag(*grab)
        return Done("confirmed" if self.drag.down_ok else "failed", "trim.begin",
                    f"grabbed the {self.edge} edge")

    def update(self, screen_x: float) -> None:
        if self.drag is not None:
            self.drag.move(screen_x)

    def finish(self, *, auto_undo: bool = True, tolerance: float = 3.0) -> Done:
        if self.drag is None or self.before is None:
            return Done("failed", "trim", "no drag in progress")
        self.drag.up()
        self.drag = None
        bx, by, bw, bh = self.before
        after = find_region(self.win, track=self.region.get("track"), name=self.region.get("name"))
        rect = _rect(after) if after else None
        if rect is None:
            return Done("uncertain", "trim", "cannot re-read the region to verify")
        ax_, ay, aw, ah = rect
        other = ax_ - bx if self.edge == "right" else (ax_ + aw) - (bx + bw)
        if abs(other) > tolerance:
            if auto_undo:
                keys.press("cmd+z", pid=self.pid)
            return Done("failed", "trim", "the whole region moved; that was not a trim"
                                          f"{'; undone' if auto_undo else ''}")
        if abs(aw - bw) < tolerance:
            return Done("uncertain", "trim", "nothing moved; the grab probably missed the edge")
        return Done("confirmed", "trim", f"{bw:.0f} -> {aw:.0f} px wide", value=aw)
