"""Screen channel: pixel readback for things Accessibility can't see (track colors). Needs Screen Recording permission.

Logic exposes no color attribute in its AX tree (measured by the reference projects on 12.x). But the track header
paints the color as the icon tile, and the palette draws a white ring around the selected swatch — so we capture
screen rects and read pixels. Every capture is normalised to BGRA-8888 so byte order never depends on the source.
"""
from __future__ import annotations

import colorsys
from collections import Counter
from dataclasses import dataclass

import ApplicationServices as AS
import Quartz as Q

from . import ax


def permitted(prompt: bool = False) -> bool:
    if Q.CGPreflightScreenCaptureAccess():
        return True
    return bool(Q.CGRequestScreenCaptureAccess()) if prompt else False


def element_rect(el) -> tuple[float, float, float, float] | None:
    p, s = ax.attr(el, "AXPosition"), ax.attr(el, "AXSize")
    if p is None or s is None:
        return None
    okp, pt = AS.AXValueGetValue(p, AS.kAXValueCGPointType, None)
    oks, sz = AS.AXValueGetValue(s, AS.kAXValueCGSizeType, None)
    if not (okp and oks):
        return None
    return (float(pt.x), float(pt.y), float(sz.width), float(sz.height))


def element_point(el) -> tuple[float, float] | None:
    p = ax.attr(el, "AXPosition")
    if p is None:
        return None
    okp, pt = AS.AXValueGetValue(p, AS.kAXValueCGPointType, None)
    return (float(pt.x), float(pt.y)) if okp else None


@dataclass
class Grab:
    buf: bytes
    bpr: int
    W: int
    H: int
    scale: float
    x0: float  # screen-space origin of the captured image (after clipping to the display)
    y0: float

    def rgb(self, sx: float, sy: float) -> tuple[int, int, int] | None:
        ix, iy = int((sx - self.x0) * self.scale), int((sy - self.y0) * self.scale)
        if not (0 <= ix < self.W and 0 <= iy < self.H):
            return None
        i = iy * self.bpr + ix * 4
        return (self.buf[i + 2], self.buf[i + 1], self.buf[i])  # BGRA -> (r, g, b)

    def hex(self, sx: float, sy: float) -> str | None:
        c = self.rgb(sx, sy)
        return "%02x%02x%02x" % c if c else None


def _display_for(x: float, y: float, w: float, h: float):
    main = Q.CGMainDisplayID()  # must come first: CGGetDisplaysWithRect as the first CG call aborts with CGS_REQUIRE_INIT
    err, ids, n = Q.CGGetDisplaysWithRect(Q.CGRectMake(x, y, w, h), 8, None, None)
    did = ids[0] if err == 0 and n else main
    b = Q.CGDisplayBounds(did)
    return did, (b.origin.x, b.origin.y, b.size.width, b.size.height)


def _normalise(img) -> tuple[bytes, int, int, int]:
    """Redraw any CGImage into a BGRA-8888 little-endian premultiplied-first context."""
    W, H = Q.CGImageGetWidth(img), Q.CGImageGetHeight(img)
    cs = Q.CGColorSpaceCreateDeviceRGB()
    ctx = Q.CGBitmapContextCreate(None, W, H, 8, W * 4, cs, Q.kCGImageAlphaPremultipliedFirst | Q.kCGBitmapByteOrder32Little)
    Q.CGContextDrawImage(ctx, Q.CGRectMake(0, 0, W, H), img)
    out = Q.CGBitmapContextCreateImage(ctx)
    buf = bytes(Q.CGDataProviderCopyData(Q.CGImageGetDataProvider(out)))
    return buf, Q.CGImageGetBytesPerRow(out), W, H


def _looks_black(buf: bytes, bpr: int, W: int, H: int, samples: int = 25) -> bool:
    if W == 0 or H == 0:
        return True
    for k in range(samples):
        x = (k * 7919) % W; y = (k * 104729) % H
        i = y * bpr + x * 4
        if buf[i] or buf[i + 1] or buf[i + 2]:
            return False
    return True


def grab(x: float, y: float, w: float, h: float) -> Grab | None:
    """Capture a screen rect (points). Clips to the display it sits on; scale comes from the display, not the request."""
    did, (dx, dy, dw, dh) = _display_for(x, y, w, h)
    cx, cy = max(x, dx), max(y, dy)
    cw, ch = min(x + w, dx + dw) - cx, min(y + h, dy + dh) - cy
    if cw <= 0 or ch <= 0:
        return None
    scale = Q.CGDisplayPixelsWide(did) / dw if dw else 1.0
    img = Q.CGWindowListCreateImage(Q.CGRectMake(cx, cy, cw, ch), Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID, Q.kCGWindowImageDefault)
    data = _normalise(img) if img is not None else None
    if data is None or _looks_black(*data):
        import os, subprocess, tempfile
        fd, path = tempfile.mkstemp(suffix=".png"); os.close(fd)
        try:
            subprocess.run(["screencapture", "-x", "-R", f"{cx},{cy},{cw},{ch}", path], check=True, timeout=5)
            src = Q.CGImageSourceCreateWithURL(Q.CFURLCreateWithFileSystemPath(None, path, Q.kCFURLPOSIXPathStyle, False), None)
            img2 = Q.CGImageSourceCreateImageAtIndex(src, 0, None) if src is not None else None
            if img2 is not None:
                data = _normalise(img2)
        except Exception:
            pass
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
    if data is None:
        return None
    buf, bpr, W, H = data
    return Grab(buf, bpr, W, H, W / cw if cw else scale, cx, cy)


def topmost_owner_pid(x: float, y: float) -> int | None:
    """PID owning the front-most normal window under a screen point (to know whether Logic is actually visible there)."""
    for d in Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionOnScreenOnly, Q.kCGNullWindowID):
        if d.get("kCGWindowLayer", 0) != 0:
            continue
        b = d.get("kCGWindowBounds", {})
        if b.get("X", 0) <= x < b.get("X", 0) + b.get("Width", 0) and b.get("Y", 0) <= y < b.get("Y", 0) + b.get("Height", 0):
            return int(d.get("kCGWindowOwnerPID", 0))
    return None


def pixel(rect, fx: float = 0.5, fy: float = 0.5) -> str | None:
    g = grab(*rect)
    return g.hex(rect[0] + fx * rect[2], rect[1] + fy * rect[3]) if g else None


def dominant_saturated(rect, min_sat: float = 0.28, min_val: float = 0.25, quant: int = 16, step: int = 2) -> dict:
    """Most common saturated color in rect (quantized). Gray UI chrome is ignored."""
    g = grab(*rect)
    if g is None:
        return {"hex": None, "fraction": 0.0, "samples": 0}
    counts: Counter = Counter(); total = 0; acc: dict = {}
    for y in range(0, g.H, step):
        row = y * g.bpr
        for x in range(0, g.W, step):
            i = row + x * 4
            b, gg, r = g.buf[i], g.buf[i + 1], g.buf[i + 2]
            total += 1
            _, s, v = colorsys.rgb_to_hsv(r / 255, gg / 255, b / 255)
            if s < min_sat or v < min_val:
                continue
            key = (r // quant, gg // quant, b // quant)
            counts[key] += 1
            a = acc.setdefault(key, [0, 0, 0]); a[0] += r; a[1] += gg; a[2] += b
    if not counts:
        return {"hex": None, "fraction": 0.0, "samples": total}
    key, n = counts.most_common(1)[0]
    r, gg, b = (c // n for c in acc[key])
    return {"hex": f"{r:02x}{gg:02x}{b:02x}", "fraction": round(n / max(total, 1), 4), "samples": total}


def color_distance(h1: str | None, h2: str | None) -> float | None:
    if not h1 or not h2:
        return None
    a = tuple(int(h1[i:i + 2], 16) for i in (0, 2, 4)); b = tuple(int(h2[i:i + 2], 16) for i in (0, 2, 4))
    return sum((x - y) ** 2 for x, y in zip(a, b)) ** 0.5
