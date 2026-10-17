"""Logic-specific Accessibility map. Verified live on Logic Pro 12.3 (Creator Studio build, English UI).

Tree (main window "<project> - Tracks"):
  AXGroup "Control Bar"
    AXGroup "Control Bar" > AXGroup "Playhead Position" > AXSlider bar/beat/division/tick
                           > AXSlider "Tempo", AXPopUpButton "Time Signature"/"Key Signature"
    AXCheckBox Play/Record/Cycle/Count In/Solo/Autopunch/Replace/Tuner, view toggles (Library, Inspector, Mixer, Editors, ...)
    AXButton "Go to Beginning"
  AXGroup "Tracks" > ... > AXGroup "Tracks header" > AXLayoutItem 'Track N “Name”'
    AXRadioButton "Has Focus", AXCheckBox Mute/Solo/"Record Enable"/"Input Monitoring",
    AXSlider (pan, child indicator "<n> Pan"), AXSlider "Volume", AXTextField desc=<name>
"""
from __future__ import annotations

import re
import time

from . import ax

TRACK_RE = re.compile(r"Track (\d+) [“\"](.*)[”\"]")
TRACK_TOGGLES = {"mute": "Mute", "solo": "Solo", "arm": "Record Enable", "monitor": "Input Monitoring"}
BAR_TOGGLES = {"play": "Play", "record": "Record", "cycle": "Cycle", "count_in": "Count In", "solo": "Solo",
               "autopunch": "Autopunch", "replace": "Replace", "tuner": "Tuner", "library": "Library",
               "inspector": "Inspector", "mixer": "Mixer", "editors": "Editors", "list_editors": "List Editors",
               "smart_controls": "Smart Controls", "loop_browser": "Loop Browser", "browsers": "Browsers",
               "note_pads": "Note Pads", "toolbar": "Toolbar", "quick_help": "Quick Help"}


# ---------------------------------------------------------------- windows
def main_window(root):
    wins = ax.windows(root)
    for w in wins:
        if str(ax.title(w) or "").endswith("- Tracks"):
            return w
    for w in wins:
        if ax.attr(w, ax.A_SUBROLE) == "AXStandardWindow":
            return w
    return wins[0] if wins else None


def raise_app(root, win=None, pid: int | None = None, timeout: float = 4.0) -> bool:
    """Bring Logic to the front and its window to the top, then confirm it.

    AppleScript `activate` is not usable here: Logic ships no scripting dictionary, so the Apple Event
    goes unanswered and osascript blocks until its timeout. Worse, the caller then proceeds to click
    while some other app is still frontmost — measured: clicks meant for Logic's control bar landed in
    Final Cut Pro. NSRunningApplication alone is not enough either, because macOS' cooperative
    activation can refuse it and because being the active *app* does not raise the window above others.
    """
    import AppKit
    import ApplicationServices as AS

    def front_pid():
        a = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        return a.processIdentifier() if a else None

    if pid is None:
        pid = ax.attr(root, "AXPid") or None
    AS.AXUIElementSetAttributeValue(root, "AXFrontmost", True)
    if win is not None:
        ax.perform(win, "AXRaise")
    if pid:
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is not None:                       # IgnoringOtherApps | AllWindows
            app.activateWithOptions_((1 << 1) | (1 << 0))
    if not pid:
        return False
    end = time.time() + timeout
    while time.time() < end:
        if front_pid() == pid:
            return True
        time.sleep(0.1)
    return front_pid() == pid


def owns_point(pid: int, x: float, y: float) -> bool:
    """Whether `pid` owns the front-most normal window at a point — check before every synthetic click."""
    from . import screen
    return screen.topmost_owner_pid(x, y) == pid


def window_titles(root) -> list[str]:
    return [str(ax.title(w) or "") for w in ax.windows(root)]


def close_window(root, title: str) -> bool:
    """Close a window by title. Plug-in windows (AXDialog) expose the close box only as the window's own
    AXCloseButton attribute, not as a child, so try the attribute before scanning children."""
    for w in ax.windows(root):
        if str(ax.title(w) or "") == title:
            if (cb := ax.attr(w, "AXCloseButton")) is not None and ax.press(cb):
                return True
            cb = [c for c in ax.children(w) if ax.attr(c, ax.A_SUBROLE) == "AXCloseButton"]
            return bool(cb) and ax.press(cb[0])
    return False


# ---------------------------------------------------------------- control bar
def control_bar(win):
    return ax.find(win, role_="AXGroup", desc_="Control Bar", max_depth=2)


def bar_control(win, key: str):
    name = BAR_TOGGLES.get(key, key)
    cb = control_bar(win)
    return ax.find(cb, title_=name, max_depth=3) if cb else None


def tempo_slider(win):
    cb = control_bar(win)
    return ax.find(cb, role_="AXSlider", desc_="Tempo", max_depth=4) if cb else None


def playhead_sliders(win) -> dict:
    """bar/beat/division/tick sliders of the first 'Playhead Position' group."""
    cb = control_bar(win)
    grp = ax.find(cb, role_="AXGroup", desc_="Playhead Position", max_depth=3) if cb else None
    if grp is None:
        return {}
    return {str(ax.desc(s)): s for s in ax.children(grp) if ax.role(s) == "AXSlider"}


def transport_snapshot(win) -> dict:
    out = {}
    for k in ("play", "record", "cycle", "count_in", "solo", "autopunch", "replace"):
        el = bar_control(win, k)
        out[k] = _truthy(ax.value(el)) if el is not None else None
    t = tempo_slider(win)
    out["tempo"] = _num(ax.value(t)) if t is not None else None
    ph = playhead_sliders(win)
    out["position"] = {k: _num(ax.value(v)) for k, v in ph.items()} if ph else None
    cb = control_bar(win)
    for d in ("Time Signature", "Key Signature"):
        el = ax.find(cb, role_="AXPopUpButton", desc_=d, max_depth=4) if cb else None
        out[d.lower().replace(" ", "_")] = str(ax.value(el)) if el is not None else None
    return out


# ---------------------------------------------------------------- tracks
def tracks_header(win):
    return ax.find(win, role_="AXGroup", desc_="Tracks header", max_depth=8)


def track_items(win) -> list:
    hdr = tracks_header(win)
    return [c for c in ax.children(hdr) if ax.role(c) == "AXLayoutItem"] if hdr is not None else []


def tracks_area(win):
    """The arrange canvas ('Tracks contents'), as opposed to 'Tracks header' (the headers on the left).

    Found by description, never as "the first AXLayoutArea in the window": with the inspector open that
    is the channel-strip 'Mixer' area, and list_regions then reported inspector labels ('Read, automation
    enabled', the patch name) as regions while missing every real one.
    """
    return ax.find(win, role_="AXGroup", desc_="Tracks contents", max_depth=8)


def region_items(area) -> list:
    """One lane per track. Logic 12.3 publishes each lane as its own AXLayoutArea ('Track N “Name”')
    whose children are the regions; AXLayoutItem is kept for layouts that flatten the lanes."""
    return ([c for c in ax.children(area) if ax.role(c) in ("AXLayoutArea", "AXLayoutItem")]
            if area is not None else [])


def list_regions(win, track: int | None = None) -> list[dict]:
    """Regions on the arrange canvas, grouped by the track lane they sit in.

    Read-only, and deliberately honest about its limits:

    * Logic virtualises the canvas the same way it virtualises track rows — a region scrolled out of view
      has a name but NO position. Those are returned with `visible: False` and null geometry rather than
      being dropped, so a caller can tell "no regions" from "cannot see them from here".
    * `bar`/`length_bars` are NOT reported. Converting a screen rectangle to musical time needs the ruler's
      current zoom and scroll offset, and getting that subtly wrong yields plausible bar numbers that are
      simply untrue. Screen geometry is reported as screen geometry; anything time-accurate should come
      from the region's own AXDescription or from a channel that can prove it.
    """
    from . import screen                          # AXPosition/AXSize are wrapped AXValues; screen unwraps them

    area = tracks_area(win)
    if area is None:
        return []
    out = []
    for lane in region_items(area):
        m = TRACK_RE.match(str(ax.desc(lane) or ""))
        idx = int(m.group(1)) if m else None
        if track is not None and idx != track:
            continue
        lane_name = m.group(2) if m else str(ax.desc(lane) or "")
        for r in ax.children(lane):
            if ax.role(r) not in ("AXLayoutItem", "AXImage", "AXGroup"):
                continue
            name = ax.desc(r) or ax.title(r)
            if not name:
                continue
            rect = screen.element_rect(r)
            out.append({
                "track": idx,
                "track_name": lane_name,
                "name": str(name),
                "selected": _truthy(ax.value(r)) if ax.value(r) is not None else None,
                "visible": rect is not None,
                "screen": {"x": rect[0], "y": rect[1], "w": rect[2], "h": rect[3]} if rect else None,
            })
    return out


def _pan_slider(item):
    for c in ax.children(item):
        if ax.role(c) != "AXSlider":
            continue
        if not ax.desc(c):
            return c
        if any(str(ax.desc(k) or "").endswith("Pan") for k in ax.children(c)):
            return c
    return None


def track_info(item) -> dict:
    m = TRACK_RE.match(str(ax.desc(item) or ""))
    d: dict = {"index": int(m.group(1)) if m else None, "name": m.group(2) if m else str(ax.desc(item))}
    for key, label in TRACK_TOGGLES.items():
        el = ax.find(item, role_="AXCheckBox", desc_=label, max_depth=1)
        d[key] = _truthy(ax.value(el)) if el is not None else None
    foc = ax.find(item, role_="AXRadioButton", desc_="Has Focus", max_depth=1)
    d["selected"] = _truthy(ax.value(foc)) if foc is not None else None
    vol = ax.find(item, role_="AXSlider", desc_="Volume", max_depth=1)
    d["volume"] = _num(ax.value(vol)) if vol is not None else None
    d["volume_text"] = str(ax.attr(vol, "AXValueDescription") or "") if vol is not None else None
    pan = _pan_slider(item)
    d["pan"] = _num(ax.value(pan)) if pan is not None else None
    d["is_folder"] = ax.find(item, role_="AXDisclosureTriangle", max_depth=1) is not None
    return d


def list_tracks(win, colors: bool = False, logic_pid: int | None = None) -> list[dict]:
    """Deduplicated by track index — Logic sometimes publishes the same header element more than once,
    which made an exact selection check ([22,22,22] != [22]) fail for a selection that was in fact correct."""
    out, seen = [], set()
    for i in track_items(win):
        d = track_info(i)
        if d["index"] is not None and d["index"] in seen:
            continue
        if d["index"] is not None:
            seen.add(d["index"])
        if colors:
            d["color"] = track_color(win, i, logic_pid)
        out.append(d)
    return out


def selected_indices(win) -> list[int]:
    return sorted({t["index"] for t in list_tracks(win) if t["selected"] and t["index"] is not None})


def track_color(win, item, logic_pid: int | None = None) -> dict | None:
    """Approximate track color = the icon tile in the header (Logic paints it nowhere AX can read).
    Samples the icon band only; None when the header is scrolled/covered or another app's window is on top."""
    from . import screen
    if not screen.permitted():
        return None
    r = screen.element_rect(item)
    hdr = tracks_header(win)
    vis = screen.element_rect(hdr) if hdr is not None else None
    if r is None:
        return None
    if vis is not None:  # clip to the visible header area (mixer pane / scrolling hides rows)
        top, bottom = max(r[1], vis[1]), min(r[1] + r[3], vis[1] + vis[3])
        if bottom - top < r[3] * 0.6:
            return None
        r = (r[0], top, r[2], bottom - top)
    band = (r[0] + 44, r[1] + 6, 110, max(r[3] - 12, 8))  # icon tile lives ~54-84pt in (folder children shift by ~30)
    if logic_pid and screen.topmost_owner_pid(band[0] + band[2] / 2, band[1] + band[3] / 2) not in (None, logic_pid):
        return None
    d = screen.dominant_saturated(band)
    return {"hex": d["hex"], "confidence": d["fraction"]} if d["hex"] and d["fraction"] >= 0.05 else None


def tracks_scrollbar(win):
    """The tall vertical scrollbar of the track list."""
    from . import screen
    bars = [x for x in ax.find_all(win, lambda e: ax.role(e) == "AXScrollBar", max_depth=10, limit=10)
            if ax.attr(x, "AXOrientation") == "AXVerticalOrientation"]
    bars = [(b, screen.element_rect(b)) for b in bars]
    bars = [(b, r) for b, r in bars if r]
    return max(bars, key=lambda br: br[1][3])[0] if bars else None


def scroll_track_into_view(win, index: int, target_y: float = 480.0, tries: int = 4) -> bool:
    """Bring a track header on screen so it can be clicked.

    In a long session most headers sit far outside the viewport (negative y, or below the window), where
    clicks land nowhere and AXSelected silently refuses. Scrolling is linear in the scrollbar's 0..1 value,
    so the needed position is computed and then verified.
    """
    from . import screen
    sb = tracks_scrollbar(win)
    if sb is None:
        return False
    # The header's own rect is the SCROLLING CONTENT, whose y goes negative — the scrollbar's rect is
    # the viewport, so the visible band is taken from that.
    bar = screen.element_rect(sb)
    lo, hi = (bar[1] - 2, bar[1] + bar[3] - 22) if bar else (200.0, 1000.0)
    # Travel per unit of scroll value is (content height - viewport height). It is NOT a constant: track
    # heights change with vertical zoom, so a hardcoded figure under-scrolls badly once rows get taller
    # and the row never actually reaches the viewport.
    hdr = tracks_header(win)
    content = screen.element_rect(hdr)
    viewport = bar[3] if bar else 850.0
    slope = max((content[3] - viewport) if content else 0.0, 1.0)
    for _ in range(tries):
        item = find_track(win, index)
        if item is None:
            return False
        r = screen.element_rect(item)
        if r is None:
            return False
        if lo <= r[1] <= hi:
            return True
        cur = _num(ax.value(sb)) or 0.0
        want = max(0.0, min(1.0, cur + (r[1] - target_y) / slope))
        if abs(want - cur) < 1e-4:
            return lo <= r[1] <= hi
        ax.set_value(sb, float(want))
        time.sleep(0.35)
    item = find_track(win, index)
    r = screen.element_rect(item) if item is not None else None
    return bool(r and lo <= r[1] <= hi)


def header_point(item) -> tuple[float, float] | None:
    """A point in a track header that can only mean "select this track".

    The row is packed with live controls — mute/solo/record-arm, volume and pan sliders, the name field —
    and a click landing on any of them does that instead of selecting, silently. Row height also changes
    with vertical zoom, so a fixed offset that works at one zoom lands on a fader at another. So measure:
    take the row's mid-height and return the centre of the widest horizontal run no child covers (in
    practice the icon gap between the focus strip and the name field).
    """
    from . import screen
    r = screen.element_rect(item)
    if r is None:
        return None
    x0, y0, w, h = r
    y = y0 + h / 2
    spans = []
    for c in ax.children(item):
        if ax.role(c) == "AXSplitter":
            continue
        cr = screen.element_rect(c)
        if cr and cr[1] - 1 <= y <= cr[1] + cr[3] + 1:      # only children crossing the mid-line block it
            spans.append((cr[0] - 2, cr[0] + cr[2] + 2))
    spans.sort()
    best, cur = None, x0
    for a, b in spans:
        if a - cur > (best[1] - best[0] if best else 0):
            best = (cur, a)
        cur = max(cur, b)
    if x0 + w - cur > (best[1] - best[0] if best else 0):
        best = (cur, x0 + w)
    if best is None or best[1] - best[0] < 6:
        return None
    return ((best[0] + best[1]) / 2, y)


class TrackMoved(RuntimeError):
    """A header click dragged the track instead of selecting it. Never swallow this."""


def click_select_track(win, index: int, pid: int | None = None) -> bool:
    """Select a track by clicking its header, scrolling it into view first.

    AXPress on 'Has Focus' and writing AXSelected both report success and do nothing here, so a real
    click is the only reliable route — and a real click needs the row actually on screen.
    """
    from . import applescript, mouse, screen
    from .. import target
    app = target.find()
    if app is None or not applescript.ensure_front(app.name):
        return False                      # a click would go to whatever app is actually in front
    dismiss_menus(ax.app(app.pid))         # an open menu would swallow the click
    if not scroll_track_into_view(win, index):
        return False
    item = find_track(win, index)
    r = screen.element_rect(item)
    if item is None or r is None:
        return False
    pt = header_point(item)
    if pt is None:
        return False
    if not owns_point(pid or app.pid, *pt):    # a palette or another app over the row eats the click
        return False
    before = {k: track_info(item).get(k) for k in ("mute", "solo", "arm", "monitor")}
    order_before = [t["name"] for t in list_tracks(win)]
    mouse.click(*pt, pid=None)
    time.sleep(0.3)
    # header_point aims at the widest run no control covers -- but on a FOLDER STACK that empty run is
    # the drag handle, so the click can REORDER the track instead of selecting it. Measured 2026-09-08:
    # a folder moved below a mixer folder. Undo it immediately and fail rather than continue.
    order_after = [t["name"] for t in list_tracks(win)]
    if order_after != order_before:
        press_menu_item(ax.app(app.pid), "Edit", "~^Undo")
        time.sleep(0.6)
        restored = [t["name"] for t in list_tracks(win)] == order_before
        raise TrackMoved(f"clicking track {index} reordered the arrangement"
                         f"{'' if restored else ' AND THE UNDO DID NOT RESTORE IT'}")
    # A header layout this code has not seen can put a control under the "free" point. Measured: on a
    # narrow header the click landed on Record Enable and armed the track. Any toggle that changed is
    # put back and the selection is reported as failed rather than silently armed/muted.
    item = find_track(win, index)
    after = {k: track_info(item).get(k) for k in ("mute", "solo", "arm", "monitor")} if item is not None else before
    if after != before:
        for k in ("mute", "solo", "arm", "monitor"):
            if after.get(k) != before.get(k):
                el = track_toggle_el(item, k)
                if el is not None:
                    set_checkbox(el, bool(before.get(k)))
        return False
    return selected_indices(win) == [index]


class TargetError(ValueError):
    """A track target that must not be acted on: malformed, ambiguous or self-contradictory."""


def check_track_selectors(index, name) -> None:
    """Shape checks before any lookup. `True` is not index 1, "" is not a name, 0 is not a row."""
    if index is None and name is None:
        raise TargetError("need index or name")
    if index is not None and (isinstance(index, bool) or not isinstance(index, int) or index < 1):
        raise TargetError(f"index must be a positive integer (1 = top track), got {index!r}")
    if name is not None and (not isinstance(name, str) or not name.strip()):
        raise TargetError("name must be a non-blank string")


def resolve_track(win, index: int | None = None, name: str | None = None, fuzzy: bool = False):
    """The ONE track header that `index` and/or `name` denote, or raise TargetError.

    * index alone: that row.
    * name alone: exactly one track with that exact name (case-insensitive, outer spaces ignored).
      Two tracks called "Vocal" are an error, never "the first one" -- the first version returned the
      first match, and a mute meant for the second Vocal landed on the first.
    * both: the row at `index` must carry `name`. A mismatch is a contradiction (the list the caller
      copied from is stale, or the caller guessed), not a tie-break in favour of either selector.
      This corroboration is what a mutation should demand.
    * fuzzy: only when asked, only for a NAME-ONLY lookup that found no exact match, and only if the
      substring matches exactly one track. Mutations never ask.
    """
    check_track_selectors(index, name)
    want = name.strip().lower() if name is not None else None
    rows: list[tuple[int, str, object]] = []
    for it in track_items(win):
        m = TRACK_RE.match(str(ax.desc(it) or ""))
        if m:
            rows.append((int(m.group(1)), m.group(2), it))
    if index is not None:
        hits = [r for r in rows if r[0] == index]
        if not hits:
            raise TargetError(f"no track at index {index}")
        _, found, it = hits[0]                        # Logic can publish one header twice: same row
        if want is not None and found.strip().lower() != want:
            raise TargetError(f"track {index} is named {found!r}, not {name!r}; list the tracks again")
        return it
    exact = [r for r in rows if r[1].strip().lower() == want]
    if len(exact) == 1:
        return exact[0][2]
    if len(exact) > 1:
        raise TargetError(f"{len(exact)} tracks are named {name!r} (indexes {[r[0] for r in exact]}); use index")
    if fuzzy:
        sub = [r for r in rows if want in r[1].lower()]
        if len(sub) == 1:
            return sub[0][2]
        if len(sub) > 1:
            raise TargetError(f"{name!r} matches {len(sub)} tracks {[r[1] for r in sub]}; use the exact name or index")
    raise TargetError(f"no track named {name!r}")


def find_track(win, index: int | None = None, name: str | None = None, fuzzy: bool = True):
    """resolve_track without the reason: None for anything that is not exactly one unambiguous track."""
    try:
        return resolve_track(win, index, name, fuzzy=fuzzy)
    except TargetError:
        return None


def project_identity(win) -> dict:
    """Which document the window shows: the path Logic reports plus the title.

    Captured before a write and compared after it, so a project switched under the call is reported
    instead of silently editing whichever project is now in front."""
    return {"document": str(ax.attr(win, "AXDocument") or ""), "title": str(ax.title(win) or "")}


def selection_count(root) -> int | None:
    """Logic's own answer: open the Track menu (titles re-validate on open) and read 'Delete Track' / 'Delete N Tracks'."""
    titles, _ = open_menu(root, "Track")
    close_menu(root, "Track")
    for t in titles:
        m = re.fullmatch(r"Delete (?:(\d+) )?Tracks?", t)
        if m:
            return int(m.group(1) or 1)
    return None


def track_toggle_el(item, key: str):
    return ax.find(item, role_="AXCheckBox", desc_=TRACK_TOGGLES[key], max_depth=1)


def select_track(item) -> bool:
    foc = ax.find(item, role_="AXRadioButton", desc_="Has Focus", max_depth=1)
    return foc is not None and ax.press(foc)


# ---------------------------------------------------------------- generic verified writes
def set_checkbox(el, on: bool, settle: float = 0.35) -> dict:
    """Press, verify; fall back to AXValue write. Returns {'ok', 'before', 'after', 'how'}."""
    before = _truthy(ax.value(el))
    if before == on:
        return {"ok": True, "before": before, "after": before, "how": "noop"}
    ax.press(el); time.sleep(settle)
    after = _truthy(ax.value(el))
    if after == on:
        return {"ok": True, "before": before, "after": after, "how": "press"}
    ax.set_value(el, 1 if on else 0); time.sleep(settle)
    after = _truthy(ax.value(el))
    return {"ok": after == on, "before": before, "after": after, "how": "set_value"}


def set_slider(el, value: float, settle: float = 0.3) -> dict:
    before = _num(ax.value(el))
    ok = ax.set_value(el, float(value))
    if not ok:
        ok = ax.set_value(el, int(value))
    time.sleep(settle)
    after = _num(ax.value(el))
    return {"ok": ok and after is not None and abs(after - float(value)) < 0.51, "before": before, "after": after}


# ---------------------------------------------------------------- menus (robust alternative to key chords)
def _submenu(node) -> list:
    kids = ax.children(node)
    return ax.children(kids[0]) if kids else []


def find_menu_item(root, *path):
    """path items are exact titles or regex strings prefixed with '~' (e.g. '~^Delete 1 Track$')."""
    node = ax.attr(root, ax.A_MENUBAR)
    if node is None:
        return None
    items = ax.children(node)
    for i, p in enumerate(path):
        hit = None
        for it in items:
            t = str(ax.title(it) or "")
            if (p.startswith("~") and re.search(p[1:], t)) or t == p:
                hit = it
                break
        if hit is None:
            return None
        node = hit
        if i < len(path) - 1:
            items = _submenu(node)
    return node


def menu_items(root, *path) -> list[str]:
    node = find_menu_item(root, *path) if path else ax.attr(root, ax.A_MENUBAR)
    if node is None:
        return []
    kids = _submenu(node) if path else ax.children(node)
    return [str(ax.title(k)) for k in kids if ax.title(k)]


def press_menu_item(root, *path) -> bool:
    """Press a menu item, opening its menu first if a cold press is refused.

    Some Logic items (Edit > Undo is the one that caught us) are found by find_menu_item and report
    AXEnabled, but AXPress on them returns False until Logic has actually opened and re-validated the
    menu. Falling back to open-then-press makes those work; the menu is always closed again on failure,
    because a menu left hanging covers the track headers and swallows every later click.
    """
    el = find_menu_item(root, *path)
    if el is None:
        return False
    if ax.press(el):
        return True
    if len(path) < 2:
        return False
    titles, els = open_menu(root, path[0])
    node, want = None, path[1]
    for t, cand in zip(titles, els):
        if (want.startswith("~") and re.search(want[1:], t)) or t == want:
            node = cand
            break
    if node is None:
        close_menu(root, path[0])
        return False
    for p in path[2:]:                      # walk any remaining submenu levels on the live menu
        nxt = None
        for k in _submenu(node):
            t = str(ax.title(k) or "")
            if (p.startswith("~") and re.search(p[1:], t)) or t == p:
                nxt = k
                break
        if nxt is None:
            close_menu(root, path[0])
            return False
        node = nxt
    if ax.press(node):
        return True
    close_menu(root, path[0])
    return False


def open_menu(root, title: str, settle: float = 0.25) -> tuple[list[str], list]:
    """AXPress the menubar item so Logic re-validates titles ('Delete Track' vs 'Delete 8 Tracks'), then read them.
    Caller must either press one of the returned elements or call close_menu()."""
    m = find_menu_item(root, title)
    if m is None:
        return [], []
    ax.press(m); time.sleep(settle)
    kids = [k for k in _submenu(m) if ax.title(k)]
    return [str(ax.title(k)) for k in kids], kids


def close_menu(root, title: str | None = None) -> bool:
    """Cancel the AXMenu itself, not the menubar item.

    Cancelling the menubar item reports success and leaves the menu on screen, where it covers the track
    headers and swallows every subsequent click — this is what silently broke a 131-track rename after the
    first failed menu match.
    """
    bar = ax.attr(root, ax.A_MENUBAR)
    if bar is None:
        return True
    closed = True
    for item in ax.children(bar):
        if title is not None and str(ax.title(item) or "") != title:
            continue
        for menu in ax.children(item):
            if ax.role(menu) == "AXMenu":
                if not ax.perform(menu, "AXCancel"):
                    closed = False
                time.sleep(0.15)
    return closed


def dismiss_menus(root, tries: int = 2) -> bool:
    """Make sure no menu is left hanging over the UI before clicking anything."""
    for _ in range(tries):
        bar = ax.attr(root, ax.A_MENUBAR)
        if bar is None:
            return True
        if not any(ax.attr(i, "AXSelected") for i in ax.children(bar)):
            return True
        close_menu(root)
    bar = ax.attr(root, ax.A_MENUBAR)
    return not any(ax.attr(i, "AXSelected") for i in ax.children(bar or []))


def press_fresh_menu_item(root, menu_title: str, pattern: str) -> tuple[bool, list[str]]:
    """Open the menu for real, press the first item whose title matches regex `pattern`; else close. Returns (pressed, titles)."""
    titles, els = open_menu(root, menu_title)
    for t, el in zip(titles, els):
        if re.search(pattern, t):
            return ax.press(el), titles
    close_menu(root, menu_title)          # must actually close, or it covers everything beneath
    return False, titles


def find_window(root, title: str):
    for w in ax.windows(root):
        if str(ax.title(w) or "") == title:
            return w
    return None


def wait_window(root, title: str, timeout: float = 2.0):
    end = time.time() + timeout
    while time.time() < end:
        w = find_window(root, title)
        if w is not None:
            return w
        time.sleep(0.1)
    return None


def nudge_slider(el, target: float, max_steps: int = 300) -> dict:
    """For sliders that only expose AXIncrement/AXDecrement (Logic's track volume/pan, playhead)."""
    before = _num(ax.value(el)); cur = before
    steps = 0
    while cur is not None and abs(cur - target) >= 0.5 and steps < max_steps:
        ax.perform(el, "AXIncrement" if cur < target else "AXDecrement")
        nxt = _num(ax.value(el)); steps += 1
        if nxt == cur:  # hit a bound
            break
        cur = nxt
    return {"ok": cur is not None and abs(cur - target) < 0.5, "before": before, "after": cur, "steps": steps}


def nudge_slider_to_db(el, target_db: float, tolerance: float = 0.3, max_steps: int = 400) -> dict:
    """Drive a fader to a dB target using its own dB readout — the fallback when the MCU cannot reach it."""
    def read():
        return parse_db(ax.attr(el, "AXValueDescription"))
    before = read()
    cur = before
    steps = 0
    while cur is not None and abs(cur - target_db) > tolerance and steps < max_steps:
        ax.perform(el, "AXIncrement" if cur < target_db else "AXDecrement")
        nxt = read(); steps += 1
        if nxt is None or nxt == cur:
            break
        if (target_db - nxt > 0) != (target_db - cur > 0):   # crossed the target; take the nearer side
            cur = nxt
            break
        cur = nxt
    return {"ok": cur is not None and abs(cur - target_db) <= tolerance, "before_db": before,
            "after_db": cur, "display": ax.attr(el, "AXValueDescription"), "steps": steps}


def parse_db(text: str | None) -> float | None:
    if not text:
        return None
    t = str(text).replace("dB", "").strip()
    if "∞" in t or "inf" in t.lower():
        return float("-inf")
    try:
        return float(t.replace("+", ""))
    except ValueError:
        return None


def track_db(item) -> float | None:
    vol = ax.find(item, role_="AXSlider", desc_="Volume", max_depth=1)
    return parse_db(ax.attr(vol, "AXValueDescription")) if vol is not None else None


# ---------------------------------------------------------------- helpers
def _truthy(v) -> bool | None:
    if v is None:
        return None
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off"):
        return False
    try:
        return float(s) != 0.0
    except ValueError:
        return None


def _num(v) -> float | None:
    try:
        return float(str(v))
    except (TypeError, ValueError):
        return None


def rename_editor(root, win, index: int, timeout: float = 1.4):
    """The live inline rename editor for `index`, or None.

    Two traps. The row's own AXTextField child is the *display label*, not the editor — it reports
    AXFocused False and AXValue 0 even while the editor is open, so checking it says "no editor" forever.
    The real editor is the application's AXFocusedUIElement. And because that is app-global, it must be
    proved to belong to the intended row before anything is written, or a rename lands on another track.
    """
    from . import screen
    import ApplicationServices as AS
    end = time.time() + timeout
    while time.time() < end:
        err, el = AS.AXUIElementCopyAttributeValue(root, "AXFocusedUIElement", None)
        if not err and el is not None and ax.role(el) == "AXTextField" and ax.attr(el, "AXFocused"):
            # Opening the editor can scroll the list, so the row's rect must be re-read now, not before
            # the menu press — a stale rect rejects a perfectly good editor that simply moved.
            item = find_track(win, index)
            row, er = screen.element_rect(item), screen.element_rect(el)
            if row and er and row[1] - 4 <= er[1] <= row[1] + row[3] + 4:
                return el
            if item is not None and str(ax.value(el) or "") == str(track_info(item).get("name") or ""):
                return el                      # same text as the row we asked for: it is that row's editor
        time.sleep(0.1)
    return None


def rename_track_safe(root, win, index: int, new_name: str, pid: int) -> dict:
    """Rename a track by writing the editor's value — never by typing.

    Typing into Logic does not fail quietly: if the editor is not actually open every character becomes a
    key command, and one such slip started a recording. Writing AXValue on the focused editor and
    confirming it avoids the keyboard altogether, so a missing editor is a no-op instead of a hazard.
    """
    from . import keys
    if not click_select_track(win, index, pid=pid):
        return {"ok": False, "reason": "could not select", "typed": False}
    if selected_indices(win) != [index]:
        return {"ok": False, "reason": "selection did not match", "typed": False}

    pressed, titles = press_fresh_menu_item(root, "Track", r"^Rename (Track|1 Track)$")
    if not pressed:
        close_menu(root)
        return {"ok": False, "reason": f"menu offered {[t for t in titles if t.startswith('Rename')]}", "typed": False}

    field = rename_editor(root, win, index)
    if field is None:
        close_menu(root)
        keys.press("escape", pid)
        return {"ok": False, "reason": "rename editor never opened on this row", "typed": False}

    if not ax.set_value(field, new_name):
        keys.press("escape", pid)
        return {"ok": False, "reason": "editor rejected the new value", "typed": False}
    ax.perform(field, "AXConfirm")
    time.sleep(0.35)
    item = find_track(win, index)
    got = track_info(item)["name"] if item is not None else None
    if got != new_name:
        keys.press("escape", pid)
    return {"ok": got == new_name, "name": got, "typed": False}


def press_osc_connect(root) -> bool:
    """Click "Connect" on Logic's alert 'OSC app "TouchOSC" on "<name>" wants to connect to Logic Pro.'"""
    for w in ax.windows(root):
        for _, b in ax.walk(w, 8):
            if ax.role(b) == "AXButton" and ax.title(b) == "Connect":
                return ax.press(b)
    return False
