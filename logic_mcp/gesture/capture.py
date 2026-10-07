"""Record labelled hand samples, at the human's pace.

The first attempt at tuning thresholds against a real hand failed for a boring reason: the script
announced "do a fist now" on its own timetable while the instructions reached the human seconds later,
so every label was attached to whatever they happened to be doing at the time. Garbage labels produce
garbage thresholds, and the numbers looked like the classifier was broken when the harness was.

So this waits. It prompts, the human presses Return when their hand is actually in the pose, and only
then does it record. It also refuses to record a sample where the hand is missing, low-confidence, or
against the edge of the frame, because those were the other half of the bad data.

Output is one JSON object per frame in a JSONL file: the raw 21 landmarks plus the label. That is
deliberately the raw landmarks and not derived ratios — it can be replayed against any future classifier,
including a learned one, without recapturing.
"""
from __future__ import annotations

import argparse
import json
import sys
import time

from . import protocol as P

POSES = ["open_palm", "fist", "pinch", "point", P.NO_POSE]

DESCRIPTIONS = {
    "open_palm": "Open palm. Fingers spread, palm facing the camera.",
    "fist": "Fist. Curl all four fingers in; thumb wherever is natural.",
    "pinch": "Pinch. Thumb and index tips TOUCHING, other three fingers out straight.",
    "point": "Point. Index finger out, the other three curled.",
    P.NO_POSE: "Relaxed. Half-curled, deliberately not any pose — this teaches it to say 'nothing'.",
}

# A hand this close to the frame edge is partly outside it, and its fingertip landmarks are guesses.
EDGE_MARGIN = 0.06
MIN_SCORE = 0.7


def quality(f: P.HandFrame) -> tuple[bool, str]:
    if not f.hands:
        return False, "no hand in frame"
    h = f.primary
    if h.score < MIN_SCORE:
        return False, f"low confidence ({h.score:.2f})"
    xs = [p[0] for p in h.lm]
    ys = [p[1] for p in h.lm]
    if min(xs) < EDGE_MARGIN or max(xs) > 1 - EDGE_MARGIN:
        return False, "hand is against the left/right edge of frame"
    if min(ys) < EDGE_MARGIN or max(ys) > 1 - EDGE_MARGIN:
        return False, "hand is against the top/bottom edge — hold it higher and further back"
    return True, ""


def record(rx: P.UdpReceiver, label: str, seconds: float, out) -> int:
    end = time.monotonic() + seconds
    n = 0
    rejected: dict[str, int] = {}
    while time.monotonic() < end:
        time.sleep(1 / 60)
        for o in rx.drain(latest_only=True):
            f = P.HandFrame.from_obj(o)
            ok, why = quality(f)
            if not ok:
                rejected[why] = rejected.get(why, 0) + 1
                continue
            h = f.primary
            out.write(json.dumps({"label": label, "chirality": h.chirality,
                                  "score": h.score, "lm": h.lm}) + "\n")
            n += 1
    if rejected:
        worst = max(rejected.items(), key=lambda kv: kv[1])
        print(f"    ({worst[1]} frames skipped: {worst[0]})", file=sys.stderr)
    return n


def main() -> None:
    ap = argparse.ArgumentParser(description="Record labelled hand poses for tuning or training.")
    ap.add_argument("--out", default="hand-samples.jsonl")
    ap.add_argument("--seconds", type=float, default=4.0, help="recording time per pose")
    ap.add_argument("--rounds", type=int, default=2, help="how many times through the pose list")
    ap.add_argument("--port", type=int, default=P.PORT_HANDS)
    args = ap.parse_args()

    rx = P.UdpReceiver(args.port)
    print("Checking the camera stream...", file=sys.stderr)
    time.sleep(1.0)
    if not rx.drain():
        print("Nothing arriving on udp/%d. Is handd running?" % args.port, file=sys.stderr)
        raise SystemExit(1)

    total = 0
    with open(args.out, "w") as fh:
        for r in range(args.rounds):
            print(f"\n=== round {r + 1} of {args.rounds} ===", file=sys.stderr)
            for pose in POSES:
                print(f"\n  {pose.upper()}", file=sys.stderr)
                print(f"  {DESCRIPTIONS[pose]}", file=sys.stderr)
                input("  Get into the pose, then press Return (hand stays still while it records)... ")
                print(f"  recording {args.seconds:g}s...", file=sys.stderr)
                n = record(rx, pose, args.seconds, fh)
                total += n
                print(f"  got {n} good frames", file=sys.stderr)
    rx.close()
    print(f"\nWrote {total} frames to {args.out}", file=sys.stderr)
    print("Done — tell Claude and it will tune the thresholds against this.", file=sys.stderr)


if __name__ == "__main__":
    main()
