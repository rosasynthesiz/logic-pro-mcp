"""A synthetic hand, so the whole pipeline can be exercised with no camera and no Logic.

This is the Phase 0 test rig. It speaks exactly the wire format `handd` speaks, on the same UDP port, so
`gestured` cannot tell the difference — which is the point: every bug it finds is a real bug in the
consumer, not an artefact of the harness.

A script is a list of (duration, pose, value) steps. `value` drives the wrist up and down inside the
calibration box, so a step like (2.0, "pinch", 0.8) means "hold the clutch for two seconds with the hand
high", which downstream must turn into a fader at 80%.
"""
from __future__ import annotations

import argparse
import math
import sys
import time

from .. import protocol as P

# A neutral open hand at rest, wrist-centred, in normalised frame coordinates (origin top-left).
# Fingers point up, so tips have SMALLER y than their knuckles.
_REST = [
    [0.50, 0.80, 0.0],                                              # wrist
    [0.43, 0.76, 0.0], [0.39, 0.70, 0.0], [0.36, 0.65, 0.0], [0.34, 0.60, 0.0],   # thumb
    [0.46, 0.63, 0.0], [0.45, 0.55, 0.0], [0.45, 0.50, 0.0], [0.45, 0.45, 0.0],   # index
    [0.50, 0.62, 0.0], [0.50, 0.53, 0.0], [0.50, 0.47, 0.0], [0.50, 0.42, 0.0],   # middle
    [0.54, 0.63, 0.0], [0.55, 0.55, 0.0], [0.55, 0.50, 0.0], [0.55, 0.45, 0.0],   # ring
    [0.58, 0.66, 0.0], [0.60, 0.60, 0.0], [0.60, 0.56, 0.0], [0.60, 0.52, 0.0],   # pinky
]

# How far each finger's joints travel toward the palm when curled. Index 0 is the wrist and never moves.
_CURL_GROUPS = {"thumb": (1, 2, 3, 4), "index": (5, 6, 7, 8), "middle": (9, 10, 11, 12),
                "ring": (13, 14, 15, 16), "pinky": (17, 18, 19, 20)}


def _curl(lm: list[list[float]], finger: str, amount: float) -> None:
    """Pull a finger toward the wrist. amount 0 = fully extended, 1 = fully curled."""
    wx, wy = lm[0][0], lm[0][1]
    for j, i in enumerate(_CURL_GROUPS[finger]):
        # Distal joints curl further than proximal ones, which is what a real finger does.
        k = amount * (0.25 + 0.25 * j)
        lm[i][0] += (wx - lm[i][0]) * k
        lm[i][1] += (wy - lm[i][1]) * k


def _fold_thumb(lm: list[list[float]]) -> None:
    """Lay the thumb across the palm, the way it actually sits in a fist.

    `_curl` pulls a joint toward the wrist, which is right for the four fingers and wrong for the thumb:
    a folded thumb wraps OVER the curled fingers toward the pinky side, so its tip ends up near the
    pinky knuckles, not down by the wrist. The classifier measures exactly that (thumb tip to pinky MCP),
    so a wrist-ward curl left the thumb still reading as extended and no fist ever classified.
    """
    px, py = lm[P.PINKY_MCP][0], lm[P.PINKY_MCP][1]
    ix, iy = lm[P.INDEX_MCP][0], lm[P.INDEX_MCP][1]
    lm[P.THUMB_TIP] = [px - (px - ix) * 0.30, py + 0.01, 0.0]
    lm[P.THUMB_IP] = [px - (px - ix) * 0.62, py + 0.03, 0.0]
    lm[P.THUMB_MCP] = [px - (px - ix) * 0.95, py + 0.06, 0.0]


def make_hand(pose: str = "open_palm", height: float = 0.5, x: float = 0.5,
              chirality: str = "right", score: float = 0.95, gap: float = 0.08) -> P.Hand:
    """Build one landmark set for a named pose. `height` 0..1 places the wrist inside the box, with 1
    at the top — so it reads the way a fader does, not the way screen coordinates do.

    `gap` is the thumb-to-index distance for the pinch pose, as a fraction of the reference span. It
    defaults to CLOSED, because a pinch is the clutch and a half-open one is not a clutch — an earlier
    default of 0.5 produced a hand ~31% of a span open, which the classifier rightly refused. Open it
    deliberately when exercising the pinch-gap axis.
    """
    lm = [list(p) for p in _REST]

    if pose == "fist":
        for f in ("index", "middle", "ring", "pinky"):
            _curl(lm, f, 0.95)
        _fold_thumb(lm)
    elif pose == "point":
        for f in ("middle", "ring", "pinky"):
            _curl(lm, f, 0.95)
        _fold_thumb(lm)
    elif pose == "pinch":
        # Thumb tip and index tip meet; the other three stay out. `gap` opens the pinch, which is its
        # own continuous axis.
        _curl(lm, "index", 0.35)
        tx, ty = lm[P.INDEX_TIP][0], lm[P.INDEX_TIP][1]
        span = 0.10 * max(0.0, min(1.0, gap))
        lm[P.THUMB_TIP] = [tx - span, ty + span * 0.5, 0.0]
        lm[P.THUMB_IP] = [tx - span - 0.03, ty + span * 0.5 + 0.05, 0.0]
    elif pose == "open_palm" or pose == "swipe_left" or pose == "spread":
        pass
    elif pose == P.NO_POSE:
        for f in ("index", "middle", "ring", "pinky"):
            _curl(lm, f, 0.5)           # half-curled: deliberately unclassifiable

    # Place the wrist. Screen y grows downward, so a high hand has a small y.
    dy = (1.0 - max(0.0, min(1.0, height))) - lm[0][1]
    dx = max(0.0, min(1.0, x)) - lm[0][0]
    for p in lm:
        p[0] += dx
        p[1] += dy
    return P.Hand(chirality=chirality, score=score, lm=lm)


def frame(pose: str = "open_palm", height: float = 0.5, x: float = 0.5, hands: int = 1,
          gap: float = 0.08, t: float | None = None) -> P.HandFrame:
    hs = []
    if hands >= 1:
        hs.append(make_hand(pose, height, x, "right", 0.95, gap))
    if hands >= 2:
        hs.append(make_hand(pose, height, 1.0 - x, "left", 0.9, gap))
    return P.HandFrame(t=t if t is not None else P.now(), hands=hs, source="fake")


# A scripted rehearsal of the whole Phase 3 story, used by `--script demo`.
DEMO = [
    (1.0, P.NO_POSE, 0.5),      # hand present, nothing claimed
    (0.6, "open_palm", 0.5),    # play
    (1.2, P.NO_POSE, 0.5),
    (3.0, "pinch", 0.80),       # clutch closed — ride the fader up
    (2.0, "pinch", 0.35),       # ...and back down
    (1.0, P.NO_POSE, 0.35),     # release: the value must HOLD at 0.35
    (0.6, "fist", 0.35),        # stop
    (1.0, P.NO_POSE, 0.35),
    (0.6, "swipe_left", 0.35),  # undo
    (1.0, P.NO_POSE, 0.35),
]


def play(script, port: int = P.PORT_HANDS, fps: float = 60.0, loop: bool = False,
         verbose: bool = True) -> None:
    tx = P.sender(port)
    dt = 1.0 / fps
    try:
        while True:
            for dur, pose, height in script:
                end = time.monotonic() + dur
                if verbose:
                    print(f"  {dur:4.1f}s  {pose:<10} height={height:.2f}", file=sys.stderr)
                while time.monotonic() < end:
                    # A gentle wobble, so the smoothing filters have something real to chew on.
                    wob = 0.004 * math.sin(time.monotonic() * 7.0)
                    tx.send(frame(pose, height + wob, 0.5,
                                  hands=2 if pose == "spread" else 1).to_json())
                    time.sleep(dt)
            if not loop:
                break
    except KeyboardInterrupt:
        pass
    finally:
        tx.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Synthetic hand source: speaks handd's wire format.")
    ap.add_argument("--port", type=int, default=P.PORT_HANDS)
    ap.add_argument("--fps", type=float, default=60.0)
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--hold", metavar="POSE", help="hold one pose forever instead of the demo script")
    ap.add_argument("--height", type=float, default=0.5)
    args = ap.parse_args()
    script = [(1e9, args.hold, args.height)] if args.hold else DEMO
    print(f"fake hand -> udp/{args.port} at {args.fps:g} fps", file=sys.stderr)
    play(script, args.port, args.fps, args.loop)


if __name__ == "__main__":
    main()
