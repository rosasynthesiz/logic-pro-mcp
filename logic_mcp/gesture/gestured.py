"""`gestured` — the daemon that turns hands and speech into Logic moves.

It owns the arbiter and nothing else of consequence: the camera lives in `handd`, the microphone in
`voiced`, and the OSC surface in `logicpadd`. Four processes rather than one because each of those three
can hang, crash or sit behind a permission prompt, and none of them should be able to take the control
loop down with it.

The loop runs at `--rate` (default 120 Hz, i.e. twice the camera) and does four things in order:

    1. take the NEWEST hand frame, classify the pose, compute the axis value
    2. take any finalised utterances, parse them, feed the arbiter
    3. tick the arbiter so stale bindings expire
    4. execute whatever the arbiter handed back, then publish the HUD

No model is involved anywhere in step 1 or 4. The only place a model can appear is the grammar miss in
step 2, and even there it runs out-of-band in a subprocess so a slow answer cannot stall the hand.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass

from . import protocol as P
from .actions import Executor
from .arbiter import Arbiter
from .sources.handd import HandStream

try:
    from . import grammar
except ImportError:                     # built separately; the daemon still runs hand-only without it
    grammar = None                      # type: ignore[assignment]
try:
    from . import poses as poses_mod
except ImportError:
    poses_mod = None                    # type: ignore[assignment]
try:
    from . import mapping as mapping_mod
except ImportError:
    mapping_mod = None                  # type: ignore[assignment]


@dataclass(slots=True)
class Stats:
    frames: int = 0
    intents: int = 0
    axis_sends: int = 0
    commands: int = 0
    misses: int = 0
    started: float = 0.0


class ModelFallback:
    """Phase 4: what happens when the grammar does not recognise an utterance.

    Runs a user-supplied command with the raw text on stdin and expects one Intent JSON object on
    stdout. It runs on its own thread with a hard timeout, because the whole design rests on the model
    never being able to stall the hand — a fallback that blocks the loop would undo that in one line.
    """

    def __init__(self, cmd: str | None, timeout: float = 8.0):
        self.cmd = cmd
        self.timeout = timeout
        self.pending = 0
        self.results: list[P.Intent] = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cmd)

    def ask(self, text: str) -> None:
        if not self.cmd:
            return
        self.pending += 1
        threading.Thread(target=self._run, args=(text,), daemon=True).start()

    def _run(self, text: str) -> None:
        try:
            r = subprocess.run(self.cmd, shell=True, input=text, capture_output=True,
                               text=True, timeout=self.timeout)
            out = (r.stdout or "").strip().splitlines()
            if out:
                obj = json.loads(out[-1])
                intent = P.Intent.from_obj(obj)
                intent.source = "model"
                intent.raw = text
                with self._lock:
                    self.results.append(intent)
        except Exception as e:
            print(f"[gestured] model fallback failed: {type(e).__name__}: {e}", file=sys.stderr)
        finally:
            self.pending -= 1

    def drain(self) -> list[P.Intent]:
        with self._lock:
            out, self.results = self.results, []
        return out


class Gestured:
    def __init__(self, pad, *, rate: float = 120.0, sink=None, logic_pid: int | None = None,
                 dry_run: bool = False, model_cmd: str | None = None, verbose: bool = True,
                 calib=None, takeover: str = "catch", classifier=None,
                 hands_port: int = P.PORT_HANDS, voice_port: int = P.PORT_VOICE,
                 hud_port: int = P.PORT_HUD):
        self.arb = Arbiter()
        self.ex = Executor(pad, sink=sink, logic_pid=logic_pid, dry_run=dry_run)
        self.hands = HandStream(port=hands_port)
        self.voice = P.UdpReceiver(voice_port)
        self.hud = P.sender(hud_port)
        self.model = ModelFallback(model_cmd)
        self.rate = rate
        self.verbose = verbose
        self.takeover_mode = takeover
        self.stats = Stats(started=time.monotonic())
        self.running = False
        self.pose = P.NO_POSE
        self._engaged_from: tuple[float, float] | None = None   # (hand value, Logic value) at engage
        self._last_sent: float | None = None                    # what the slew limiter produced last

        # An injected classifier lets the integration test drive poses directly, without a camera and
        # without depending on the real classifier's thresholds.
        self.classifier = classifier or (poses_mod.PoseClassifier() if poses_mod else None)
        if calib is not None:
            self.calib = calib
        elif mapping_mod is not None:
            # Default box: the middle of the frame, which is where a hand naturally sits. Real use wants
            # `--calibrate`, because "hand height" means nothing until the box is pinned to your desk.
            self.calib = mapping_mod.CalibBox.from_corners((0.2, 0.15), (0.8, 0.85))
        else:
            self.calib = None

    # ------------------------------------------------------------------ per-frame
    def _axis_value(self, frame: P.HandFrame) -> float | None:
        """Read whichever dimension the bound axis wants, as 0..1."""
        hand = frame.primary
        if hand is None or self.calib is None or mapping_mod is None:
            return None
        axis = self.arb.axis
        if axis == "volume":
            return self.calib.map_y(hand)
        if axis == "pan":
            return mapping_mod.roll(hand)
        if axis == "param":
            return poses_mod.pinch_gap(hand) if poses_mod else None
        if axis in ("scrub", "eq"):
            return self.calib.map_x(hand)
        return None

    def _apply_takeover(self, raw: float) -> float:
        """Stop the fader jumping when the clutch closes.

        `mapping.takeover("catch")` is a per-call slew limiter: it steps from where the fader IS toward
        where the hand asks, by at most one step. So it has to be fed the value it last produced. An
        earlier version fed it the engage-time value on every frame, which pinned the fader one step
        from where it started and left it there while the hand went wherever it liked.

        It also layered a relative (hand-delta) transform underneath, which is a different idiom
        altogether — relative takeover never jumps because position is meaningless, rate-limited
        absolute never jumps because it converges. Stacking them gave neither. This is the second,
        matching what `mapping.takeover` actually implements: absolute target, rate-limited approach
        from Logic's real current value.
        """
        if mapping_mod is None or self.takeover_mode == "jump" or self._engaged_from is None:
            return raw
        _hand0, logic0 = self._engaged_from
        current = self._last_sent if self._last_sent is not None else logic0
        out = mapping_mod.takeover(current, raw, self.takeover_mode, dt=1.0 / self.rate)
        self._last_sent = out
        return out

    # ------------------------------------------------------------------ loop
    def step(self, t: float) -> list:
        out = []
        frame, lost = self.hands.poll(t)

        if lost:
            out += self.arb.on_hands_lost(t)
            self._engaged_from = None
            self._last_sent = None
        elif frame is not None and frame.hands:
            self.stats.frames += 1
            if self.classifier is not None:
                pose = self.classifier.update(frame, t)
                if pose != self.pose:
                    was_engaged = self.arb.engaged
                    self.pose = pose
                    out += self.arb.on_pose(pose, t)
                    if self.arb.engaged and not was_engaged:
                        raw = self._axis_value(frame)
                        strip = self.ex.resolve(self.arb.target)
                        cur = self.ex.current(strip, self.arb.axis or "") if strip else None
                        self._engaged_from = (raw, cur) if (raw is not None and cur is not None) else None
                        self._last_sent = cur        # the slew starts from where Logic actually is
                    elif not self.arb.engaged:
                        self._engaged_from = None
                        self._last_sent = None
            if self.arb.engaged:
                raw = self._axis_value(frame)
                if raw is not None:
                    out += self.arb.on_value(self._apply_takeover(raw), t)

        for obj in self.voice.drain():
            u = P.Utterance.from_obj(obj)
            if not u.final or not u.text.strip():
                continue
            intent = grammar.parse(u.text) if grammar else None
            if intent is None:
                self.stats.misses += 1
                if self.model.enabled:
                    self.model.ask(u.text)
                else:
                    out += [self._note("unparsed", u.text)]
                continue
            self.stats.intents += 1
            out += self.arb.on_intent(intent, t)

        for intent in self.model.drain():
            self.stats.intents += 1
            out += self.arb.on_intent(intent, t)

        out += self.arb.tick(t)
        return out

    def _note(self, name: str, reason: str):
        from .arbiter import Action
        return Action("note", name, reason=reason)

    def execute(self, actions: list) -> None:
        for a in actions:
            done = self.ex.run(a)
            if a.kind == "axis":
                self.stats.axis_sends += 1
            elif a.kind in ("command", "transport", "key", "select"):
                self.stats.commands += 1
            if self.verbose and a.kind != "axis" and done.state != "confirmed" or \
               (self.verbose and a.kind == "note"):
                print(f"[gestured] {done}", file=sys.stderr)

    def publish(self, t: float) -> None:
        b = self.arb.binding
        readout = ""
        if b and b.value is not None and mapping_mod is not None:
            if b.axis == "volume":
                db = mapping_mod.to_db(b.value)
                readout = "-inf dB" if db is None or db < -59.9 else f"{db:+.1f} dB"
            elif b.axis == "pan":
                p = mapping_mod.to_pan(b.value)
                readout = "centre" if abs(p) < 2 else f"{'L' if p < 0 else 'R'}{abs(p)}"
            else:
                readout = f"{b.value * 100:.0f} %"
        self.hud.send(P.HudState(
            t=t, state=self.arb.state, target=self.arb.target, axis=self.arb.axis,
            value=b.value if b else None, readout=readout, pose=self.pose,
            hands=1 if self.hands.last_hand_at and (t - self.hands.last_hand_at) < 0.2 else 0,
            note="" if self.hands.alive else "no hand source",
        ).to_json())

    def run(self) -> None:
        self.running = True
        dt = 1.0 / self.rate
        next_hud = 0.0
        try:
            while self.running:
                t = P.now()
                self.execute(self.step(t))
                if t >= next_hud:            # the HUD does not need 120 Hz; 30 is plenty
                    self.publish(t)
                    next_hud = t + 1 / 30.0
                time.sleep(dt)
        except KeyboardInterrupt:
            pass
        finally:
            self.close()

    def close(self) -> None:
        self.running = False
        self.hands.close()
        self.voice.close()
        self.hud.close()


class _RecordingPad:
    """Stands in for the OSC surface so the whole pipeline can run with Logic open and untouched.

    It is NOT the same as --dry-run: dry-run still acquires the real surface (and so still needs the
    port and the advert), it just declines to write. This one never opens a socket at all.
    """

    def __init__(self, bank: list[dict] | None = None):
        self.sends: list[tuple[str, tuple]] = []
        self.presses: list[str] = []
        self._bank = bank or [{"n": n, "name": f"track {n}", "volume": 0.5, "pan": 0.5}
                              for n in range(1, 9)]

    def send(self, address, *args):
        self.sends.append((address, args))
        return True

    def press(self, address):
        self.presses.append(address)
        return True

    def snapshot(self):
        return {"bank": self._bank, "connected": True}

    def wait_for(self, address, timeout=1.5, since=None):
        return [1.0]

    @property
    def connected(self):
        return True


def parse_box(spec: str):
    """`--box x0,y0,x1,y1` in normalised camera coordinates (0..1, origin top-left, as handd emits them),
    as any two opposite corners."""
    parts = [float(p) for p in spec.split(",")]
    if len(parts) != 4 or not all(0.0 <= p <= 1.0 for p in parts):
        raise argparse.ArgumentTypeError("--box wants four numbers in 0..1: x0,y0,x1,y1")
    if mapping_mod is None:
        raise argparse.ArgumentTypeError("mapping module missing; --box is unavailable")
    return mapping_mod.CalibBox.from_corners((parts[0], parts[1]), (parts[2], parts[3]))


def calibrate(seconds: float, port: int = P.PORT_HANDS) -> dict | None:
    """Watch the wrist for `seconds` and report the box it actually covered.

    This pins the box to the desk: rest the hand where "all the way down" is, raise it to where "all the
    way up" is, sweep left and right if pan or scrub matter, and read the suggested `--box` back. Why it
    is not optional: with the default box a hand held above the chest read as <= 0.2 of fader travel
    (2026-10-06) — a MacBook camera looks up at a face, so the hands live in the lower half of the frame.
    """
    hs = HandStream(port=port)
    xs: list[float] = []
    ys: list[float] = []
    t_end = time.monotonic() + seconds
    last_print = 0.0
    print(f"[gestured] calibrating for {seconds:g}s: move your hand from your low point to your high point",
          file=sys.stderr)
    try:
        while time.monotonic() < t_end:
            frame, _lost = hs.poll(P.now())
            hand = frame.primary if frame is not None else None
            if hand is not None:
                wx, wy = hand.lm[P.WRIST][0], hand.lm[P.WRIST][1]
                xs.append(wx)
                ys.append(wy)
                if time.monotonic() - last_print > 0.5:
                    print(f"[gestured]   wrist x={wx:.2f} y={wy:.2f}", file=sys.stderr)
                    last_print = time.monotonic()
            time.sleep(1 / 60)
    finally:
        hs.close()
    if not xs:
        print("[gestured] no hand seen — is handd running and pointed at you?", file=sys.stderr)
        return None
    # Vision happily reports a wrist a little outside the frame (measured y=1.005); clamp so the printed
    # command is one parse_box will accept.
    c = lambda v: round(min(1.0, max(0.0, v)), 3)  # noqa: E731
    box = {"x0": c(min(xs)), "y0": c(min(ys)), "x1": c(max(xs)), "y1": c(max(ys))}
    print(f"[gestured] {len(xs)} frames; wrist covered x {box['x0']}..{box['x1']}, y {box['y0']}..{box['y1']}\n"
          f"[gestured] next time: gestured --box {box['x0']},{box['y0']},{box['x1']},{box['y1']}",
          file=sys.stderr)
    return box


def main() -> None:
    ap = argparse.ArgumentParser(description="gestured — hands and voice into Logic Pro")
    ap.add_argument("--rate", type=float, default=120.0)
    ap.add_argument("--dry-run", action="store_true", help="decide everything, send nothing to Logic")
    ap.add_argument("--model", metavar="CMD",
                    help="Phase 4: shell command fed unparsed speech on stdin, returning Intent JSON")
    ap.add_argument("--takeover", choices=["catch", "jump"], default="catch")
    ap.add_argument("--box", type=parse_box, metavar="x0,y0,x1,y1",
                    help="calibration box in camera coordinates (0..1, origin top-left); its bottom edge "
                         "is fader 0 and its top edge fader 1. Get the numbers from --calibrate")
    ap.add_argument("--calibrate", type=float, metavar="SECONDS",
                    help="watch the wrist for SECONDS (handd must be running) and print the --box it "
                         "covered, then exit")
    ap.add_argument("--no-daemon", action="store_true", help="bind OSC directly instead of via logicpadd")
    ap.add_argument("--fake-pad", action="store_true",
                    help="no OSC at all: log what would be sent. Phase 0 rig — safe with Logic open, "
                         "because it never touches port 9000 or the Bonjour advert")
    ap.add_argument("--bank", help="fake-pad only: comma-separated strip names, e.g. kick,snare,bass")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    if args.calibrate:
        calibrate(args.calibrate)
        return

    if args.fake_pad:
        names = [n.strip() for n in args.bank.split(",")] if args.bank else None
        bank = ([{"n": i, "name": n, "volume": 0.5, "pan": 0.5} for i, n in enumerate(names, 1)]
                if names else None)
        pad, how = _RecordingPad(bank), "fake-pad"
    else:
        from .padd import acquire
        pad, how = acquire(prefer_daemon=not args.no_daemon)
    print(f"[gestured] OSC via {how}; rate {args.rate:g} Hz; "
          f"{'DRY RUN' if args.dry_run else 'live'}", file=sys.stderr)

    g = Gestured(pad, rate=args.rate, dry_run=args.dry_run, model_cmd=args.model,
                 takeover=args.takeover, verbose=not args.quiet, calib=args.box)
    if g.classifier is None:
        print("[gestured] warning: poses module missing — hands will be ignored", file=sys.stderr)
    if g.calib is not None:
        print(f"[gestured] box x {g.calib.x0:.2f}..{g.calib.x1:.2f} y {g.calib.y0:.2f}..{g.calib.y1:.2f}"
              f"{'' if args.box is not None else ' (default — run --calibrate once)'}", file=sys.stderr)
    g.run()


if __name__ == "__main__":
    main()
