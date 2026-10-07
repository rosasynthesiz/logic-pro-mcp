"""Logic's track color palette (window 'Color'). Verified on Logic Pro 12.3 Creator Studio.

Structure: AXWindow 'Color' > AXButton desc 'close' + AXGroup > AXGroup '(null) contents' > 97 children.
  child 0        = ↺ reset button (not a color)
  children 1..96 = swatches, 4 rows x 24 columns. Only AXPosition + AXPress work; every other attribute is NoValue.
  Child order != visual order: 73-96 = top row (lightest), 1-24 = row 2 (defaults), 25-48 = row 3, 49-72 = bottom.
  AXPosition.y is the swatch's BOTTOM edge; the safe center is (x+12, y-9).
Readback: the selected swatch has a ~1px white ring just outside its rect. AXSelectedChildren is always empty.
"""
from __future__ import annotations

import re
import time

from . import ax, screen

COLS = 24
COLUMN_NAMES = ["red", "orange-red", "orange", "amber", "yellow", "yellow-green", "lime", "green", "emerald", "mint",
                "teal", "aqua", "cyan", "sky", "azure", "blue", "royal-blue", "indigo", "violet", "purple", "grape",
                "magenta", "fuchsia", "rose"]
ROW_NAMES = {1: "light", 2: "", 3: "dark", 4: "darkest"}
ALIASES = {"pink": "rose", "turquoise": "teal", "navy": "darkest blue", "brown": "darkest orange", "olive": "darkest yellow",
           "gold": "amber", "lavender": "light violet", "salmon": "light orange-red", "crimson": "dark red", "maroon": "darkest red",
           "forest": "darkest green", "sea": "teal", "ocean": "dark blue", "lemon": "light yellow", "peach": "light orange"}

_ROWS = {
    1: [(186, 70, 37), (187, 97, 41), (191, 128, 48), (195, 166, 57), (201, 201, 68), (171, 201, 66), (144, 200, 64), (120, 199, 62),
        (92, 198, 75), (92, 199, 103), (93, 199, 134), (93, 199, 166), (92, 198, 201), (75, 162, 198), (75, 148, 202), (74, 128, 205),
        (71, 101, 208), (69, 68, 209), (90, 66, 207), (111, 61, 204), (132, 53, 202), (150, 36, 194), (185, 44, 195), (185, 44, 159)],
    2: [(151, 65, 37), (153, 85, 40), (155, 109, 45), (159, 137, 52), (163, 163, 58), (142, 162, 56), (120, 162, 55), (102, 162, 55),
        (77, 160, 66), (77, 161, 90), (76, 161, 112), (77, 161, 137), (77, 160, 162), (65, 134, 160), (66, 123, 162), (69, 110, 167),
        (67, 90, 167), (67, 65, 170), (81, 64, 167), (96, 59, 165), (114, 54, 164), (125, 37, 158), (149, 42, 158), (150, 42, 132)],
    3: [(116, 56, 36), (118, 71, 38), (119, 87, 40), (122, 107, 45), (125, 125, 48), (110, 124, 48), (95, 124, 47), (82, 124, 47),
        (61, 123, 56), (62, 123, 74), (61, 123, 89), (61, 124, 107), (61, 123, 124), (54, 104, 123), (56, 97, 124), (59, 88, 127),
        (59, 75, 128), (58, 57, 128), (68, 56, 128), (79, 54, 126), (90, 49, 125), (98, 36, 121), (115, 38, 121), (115, 38, 103)],
    4: [(81, 43, 31), (82, 53, 32), (84, 64, 33), (84, 75, 35), (86, 86, 38), (77, 87, 37), (69, 87, 36), (59, 86, 36),
        (45, 85, 43), (45, 86, 54), (46, 86, 64), (45, 85, 75), (45, 85, 86), (41, 74, 85), (44, 71, 87), (46, 65, 89),
        (45, 55, 89), (46, 45, 88), (53, 45, 89), (59, 43, 89), (64, 39, 87), (69, 31, 83), (80, 32, 84), (79, 32, 73)],
}
_ROW_BASE = {1: 73, 2: 1, 3: 25, 4: 49}


MODIFIERS = {"light": 1, "pale": 1, "bright": 1, "dark": 3, "deep": 3, "darkest": 4, "darker": 4, "deepest": 4}


def index_for(row: int, col: int) -> int:
    if row not in _ROW_BASE or not 0 <= col < COLS:
        raise ValueError(f"row must be 1..4 and col 0..{COLS - 1} (got row={row} col={col})")
    return _ROW_BASE[row] + col


def row_col(index: int) -> tuple[int, int]:
    for row, base in _ROW_BASE.items():
        if base <= index < base + COLS:
            return row, index - base
    raise ValueError(f"swatch index out of range: {index}")


def rgb(index: int) -> tuple[int, int, int]:
    r, c = row_col(index)
    return _ROWS[r][c]


def hex_of(index: int) -> str:
    return "%02x%02x%02x" % rgb(index)


def name_of(index: int) -> str:
    r, c = row_col(index)
    return f"{ROW_NAMES[r]} {COLUMN_NAMES[c]}".strip()


def nearest(hexcolor: str) -> int:
    h = hexcolor.lstrip("#")
    tgt = tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))
    best, bd = 1, 1e9
    for i in range(1, 97):
        d = sum((a - b) ** 2 for a, b in zip(rgb(i), tgt))
        if d < bd:
            best, bd = i, d
    return best


def nearest_by_hue(hexcolor: str) -> int:
    """For header icon tints (lighter/desaturated than the swatch): match hue only, default row."""
    import colorsys
    h = hexcolor.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))
    hue = colorsys.rgb_to_hsv(r, g, b)[0]
    best, bd = 1, 9
    for c in range(COLS):
        cr, cg, cb = (v / 255 for v in _ROWS[2][c])
        ch = colorsys.rgb_to_hsv(cr, cg, cb)[0]
        d = min(abs(hue - ch), 1 - abs(hue - ch))
        if d < bd:
            best, bd = index_for(2, c), d
    return best


def _split_modifiers(words: list[str]) -> tuple[int | None, list[str]]:
    row, hue = None, []
    for w in words:
        if w in MODIFIERS:
            row = MODIFIERS[w]
        else:
            m = re.fullmatch(r"(light|pale|bright|dark|deep|darkest|darker|deepest)[-]?([a-z].+)", w)  # 'darkgreen', 'light-blue'
            if m:
                row = MODIFIERS[m.group(1)]; hue.append(m.group(2))
            else:
                hue.append(w)
    return row, hue


def parse(spec: str | int) -> int:
    """1..96 | '#rrggbb' / 'rrggbb' | 'blue' | 'dark green' | 'light rose' | 'navy' | 'row2 col15'."""
    if isinstance(spec, int):
        if not 1 <= spec <= 96:
            raise ValueError("swatch index must be 1..96")
        return spec
    s = str(spec).strip().lower()
    if re.fullmatch(r"#?[0-9a-f]{6}", s) and not (s.isdigit() and len(s) <= 2):
        return nearest(s)
    if s.isdigit():
        i = int(s)
        if not 1 <= i <= 96:
            raise ValueError("swatch index must be 1..96")
        return i
    m = re.fullmatch(r"row\s*(\d+)\s*[, ]?\s*col\s*(\d+)", s)
    if m:
        return index_for(int(m.group(1)), int(m.group(2)))
    words = s.replace("_", " ").replace(",", " ").split()
    row, hue_words = _split_modifiers(words)
    hue = " ".join(hue_words)
    if hue in ALIASES:  # alias may carry its own modifier ('navy' -> 'darkest blue'); an explicit user modifier wins
        arow, hue_words = _split_modifiers(ALIASES[hue].split())
        row = row if row is not None else arow
        hue = " ".join(hue_words)
    row = row or 2
    key = hue.replace(" ", "-")
    if key in COLUMN_NAMES:
        return index_for(row, COLUMN_NAMES.index(key))
    if len(key) >= 3:  # fallback: longest column name contained in the phrase (or vice versa)
        cands = [(len(n), c) for c, n in enumerate(COLUMN_NAMES) if key in n or n in key]
        if cands:
            return index_for(row, max(cands)[1])
    raise ValueError(f"unknown color {spec!r}; use 1..96, #rrggbb, or one of {COLUMN_NAMES} with light/dark/darkest")


# ---------------------------------------------------------------- live palette
def move_clear(pal, x: float = 980.0, y: float = 120.0) -> bool:
    """Park the palette away from the track headers.

    The palette floats over the arrange window. Colouring many tracks means clicking each header to select
    it, and any header that scrolls under the palette receives no click at all — measured: eighteen tracks
    silently failed until the palette was moved. Tracks at the very bottom of a long list cannot be scrolled
    clear of it, so the palette has to move instead.
    """
    import ApplicationServices as AS
    import Quartz as Q
    pt = AS.AXValueCreate(AS.kAXValueCGPointType, Q.CGPointMake(float(x), float(y)))
    return AS.AXUIElementSetAttributeValue(pal, "AXPosition", pt) == 0


def open_palette(root, timeout: float = 2.5, clear_of_tracks: bool = True):
    from . import ax_logic
    w = ax_logic.find_window(root, "Color")
    if w is None:
        if not ax_logic.press_menu_item(root, "Track", "~^Assign Track Color"):
            ax_logic.press_menu_item(root, "View", "Show Colors")
        w = ax_logic.wait_window(root, "Color", timeout)
    if w is not None and clear_of_tracks:
        move_clear(w)
        time.sleep(0.3)
    return w


def close_palette(root) -> bool:
    from . import ax_logic
    w = ax_logic.find_window(root, "Color")
    if w is None:
        return True
    for c in ax.children(w):
        if ax.role(c) == "AXButton" and str(ax.desc(c) or "") == "close":
            ax.press(c); time.sleep(0.2)
            return ax_logic.find_window(root, "Color") is None
    return False


def swatches(palette_win) -> list:
    """The 97 children of '(null) contents' (index 0 = reset button)."""
    for g in ax.children(palette_win):
        if ax.role(g) != "AXGroup":
            continue
        for gg in [g, *ax.children(g)]:
            kids = ax.children(gg)
            if len(kids) >= 97:
                return kids
    return []


def positions(kids: list) -> list[tuple[float, float] | None]:
    """AXPosition per child (AXSize is NoValue on swatches). y = bottom edge of the swatch."""
    return [screen.element_point(k) for k in kids]


def center(pos: tuple[float, float]) -> tuple[float, float]:
    return (pos[0] + 12, pos[1] - 9)


def selected_index(palette_win, kids: list | None = None) -> int | None:
    """Find the swatch with the white selection ring by sampling pixels around each swatch. None = no ring / no capture."""
    kids = kids or swatches(palette_win)
    pos = positions(kids)
    wr = screen.element_rect(palette_win)
    if wr is None:
        return None
    g = screen.grab(*wr)
    if g is None:
        return None

    def white(c):
        return c is not None and all(v >= 235 for v in c)

    best, best_hits = None, 0
    for i in range(1, min(97, len(pos))):
        p = pos[i]
        if p is None:
            continue
        x, y = p
        hits = sum(white(g.rgb(*q)) for q in ((x - 1, y - 9), (x + 25, y - 9), (x + 12, y - 20), (x + 12, y + 1)))
        if hits > best_hits:
            best, best_hits = i, hits
    return best if best_hits >= 2 else None


def press_swatch(kids: list, index: int) -> bool:
    """AXPress reports success but is inert on Logic's swatches (measured). Kept for completeness."""
    return 1 <= index <= 96 and index < len(kids) and ax.press(kids[index])


def click_swatch(kids: list, index: int, pid: int | None = None) -> bool:
    """Real mouse click at the swatch center. pid-posted first (no cursor move); caller falls back to HID if inert."""
    from . import mouse
    if not (1 <= index <= 96 and index < len(kids)):
        return False
    p = screen.element_point(kids[index])
    if p is None:
        return False
    x, y = center(p)
    return mouse.click(x, y, pid=pid)
