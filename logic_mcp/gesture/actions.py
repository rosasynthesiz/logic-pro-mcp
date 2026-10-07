"""Executing an arbiter `Action` against Logic.

This is where the two speeds of the system are kept apart.

* **Axis actions** (a hand riding a fader) go straight out as one OSC datagram. No readback, no verify,
  no envelope — at sixty a second the next frame is the verification, and Logic's own echo corrects the
  HUD. Anything slower than a socket write does not belong on this path.
* **Everything else** — transport, keys, selection, one-shot voice commands — is rare enough to afford
  the normal careful treatment, so it goes through the same channels the MCP server uses and reports
  `confirmed | uncertain | failed`.

Track resolution is deliberately fail-closed. The OSC/MCU surface only ever addresses the eight strips
Logic is currently showing, and this repo has already been bitten once by code that assumed the bank and
moved faders on tracks nobody asked for. If a spoken name is not on the surface right now, `resolve()`
returns None and the caller says so out loud rather than picking the nearest strip.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Protocol

from ..channels import keys as keys_ch
from . import protocol as P
from .arbiter import Action

# Continuous axis -> OSC address template. `{n}` is the strip on the current bank.
AXIS_ADDRESS = {
    "volume": "/1/volume{n}",
    "pan": "/2/pan{n}",
    "param": "/3/par{slot}",
}

TRANSPORT_ADDRESS = {"play": "/1/play", "stop": "/1/stop", "record": "/1/record",
                     "cycle": "/1/cycle", "rewind": "/1/rewind", "forward": "/1/forward"}

# Key chords for the pose-fired commands. `undo` is the single highest-value thing a hand can do.
KEY_CHORDS = {"undo": "cmd+z", "redo": "cmd+shift+z", "save": "cmd+s",
              "zoom_in": "cmd+right", "zoom_out": "cmd+left", "zoom": "cmd+right"}


@dataclass(slots=True)
class Done:
    """A one-line outcome. `state` follows the repo's existing envelope so gesture results read the
    same as tool results in a log."""
    state: str                    # "confirmed" | "uncertain" | "failed"
    what: str
    detail: str = ""
    value: float | None = None

    def __repr__(self) -> str:
        v = f" {self.value:.3f}" if self.value is not None else ""
        return f"<{self.state} {self.what}{v}{' — ' + self.detail if self.detail else ''}>"


class CommandSink(Protocol):
    """Where one-shot voice verbs go that this module deliberately does not reimplement.

    Mute, solo, arm, bounce, rename, plug-in inserts and the rest already exist in the MCP server with
    readback and guards that took a long time to get right. Duplicating them here would mean two
    implementations of `delete track` — so the daemon injects a sink that calls the real ones.
    """

    def __call__(self, verb: str, **kw) -> Done: ...


def _unrouted(verb: str, **kw) -> Done:
    return Done("failed", verb, "no command sink is wired; start gestured with --commands")


class Executor:
    def __init__(self, pad, sink: CommandSink | None = None, logic_pid: int | None = None,
                 dry_run: bool = False):
        self.pad = pad
        self.sink: CommandSink = sink or _unrouted
        self.pid = logic_pid
        self.dry_run = dry_run
        self.sent = 0
        self.last_axis_at = 0.0
        self._bank: list[dict] = []
        self._bank_at = 0.0
        self.log: list[Done] = []

    # ------------------------------------------------------------------ bank
    def bank(self, max_age: float = 1.0) -> list[dict]:
        """The eight strips Logic is showing, cached briefly. Read from Logic's own echo, never assumed."""
        now = time.monotonic()
        if self._bank and (now - self._bank_at) < max_age:
            return self._bank
        try:
            self._bank = self.pad.snapshot().get("bank") or []
            self._bank_at = now
        except Exception:
            pass                       # a stale bank is better than crashing the ride
        return self._bank

    def resolve(self, name: str | None) -> int | None:
        """Spoken track name -> strip 1..8, or None when that track is not on the surface.

        Matching is prefix-first then substring, both case-insensitive, and an ambiguous match refuses.
        "strings" must never quietly become "string pad" when both are on the bank.
        """
        if not name:
            return None
        want = name.strip().lower()
        if want.isdigit():
            n = int(want)
            return n if 1 <= n <= 8 else None
        rows = [(s.get("n"), str(s.get("name") or "").strip().lower()) for s in self.bank()]
        rows = [(n, nm) for n, nm in rows if nm]
        exact = [n for n, nm in rows if nm == want]
        if len(exact) == 1:
            return exact[0]
        pref = [n for n, nm in rows if nm.startswith(want)]
        if len(pref) == 1:
            return pref[0]
        sub = [n for n, nm in rows if want in nm]
        if len(sub) == 1:
            return sub[0]
        return None

    def current(self, strip: int, axis: str) -> float | None:
        """What Logic says the control sits at right now — used so engaging the clutch does not make
        the fader jump to wherever the hand happens to be."""
        key = {"volume": "volume", "pan": "pan"}.get(axis)
        if key is None:
            return None
        for s in self.bank(max_age=0.25):
            if s.get("n") == strip:
                v = s.get(key)
                return float(v) if isinstance(v, (int, float)) else None
        return None

    # ------------------------------------------------------------------ dispatch
    def run(self, a: Action) -> Done:
        fn = {"axis": self._axis, "transport": self._transport, "key": self._key,
              "select": self._select, "command": self._command, "note": self._note}.get(a.kind)
        if fn is None:
            return self._done(Done("failed", a.kind, "unknown action kind"))
        try:
            return self._done(fn(a))
        except Exception as e:                       # a bad frame must never kill the daemon
            return self._done(Done("failed", a.kind or "?", f"{type(e).__name__}: {e}"))

    def _done(self, d: Done) -> Done:
        self.log.append(d)
        if len(self.log) > 500:
            del self.log[:250]
        return d

    # ------------------------------------------------------------------ the fast path
    def _axis(self, a: Action) -> Done:
        axis, v = a.axis or "", a.value
        if v is None:
            return Done("failed", "axis", "no value")
        if axis == "scrub":
            # Scrubbing is a jog wheel, not a value. It rides MCU, not OSC, and the daemon owns the
            # rate limiting; there is no address to write here.
            return Done("uncertain", "scrub", "scrub is routed over MCU, not OSC", value=v)
        if axis == "eq":
            return Done("uncertain", "eq", "EQ needs the plug-in page open; not wired yet", value=v)
        strip = self.resolve(a.target)
        if strip is None:
            return Done("failed", axis, f"'{a.target}' is not on the surface bank right now")
        tmpl = AXIS_ADDRESS.get(axis)
        if tmpl is None:
            return Done("failed", axis, "no OSC address for this axis")
        address = tmpl.format(n=strip, slot=strip)
        if self.dry_run:
            return Done("confirmed", axis, f"dry-run {address}", value=v)
        ok = self.pad.send(address, float(v))
        self.sent += 1
        self.last_axis_at = time.monotonic()
        # No readback on purpose: at 60 Hz the next frame is the verification, and waiting on Logic's
        # echo here would put a round trip inside the fader loop.
        return Done("confirmed" if ok else "failed", axis, address, value=v)

    # ------------------------------------------------------------------ the careful path
    def _transport(self, a: Action) -> Done:
        addr = TRANSPORT_ADDRESS.get(a.name)
        if addr is None:
            return Done("failed", a.name, "unknown transport command")
        if self.dry_run:
            return Done("confirmed", a.name, f"dry-run {addr}")
        ok = self.pad.press(addr)
        if not ok:
            return Done("failed", a.name, "OSC send failed")
        echo = None
        try:
            echo = self.pad.wait_for(addr, timeout=0.8)
        except Exception:
            pass
        return Done("confirmed" if echo is not None else "uncertain", a.name,
                    addr if echo is not None else "sent, but Logic did not echo")

    def _key(self, a: Action) -> Done:
        chord = KEY_CHORDS.get(a.name)
        if chord is None:
            return Done("failed", a.name, "no key chord bound")
        if self.dry_run:
            return Done("confirmed", a.name, f"dry-run {chord}")
        ok = keys_ch.press(chord, pid=self.pid)
        # Keys have no readback of their own. `undo` is verifiable after the fact through Logic's
        # Edit menu title, but not cheaply enough to do inline.
        return Done("uncertain" if ok else "failed", a.name, chord)

    def _select(self, a: Action) -> Done:
        if a.name != "under_pointer":
            return Done("failed", a.name, "unknown select action")
        return self.sink("select_under_pointer")

    def _command(self, a: Action) -> Done:
        return self.sink(a.name, target=a.target, axis=a.axis, value=a.value, unit=a.unit)

    def _note(self, a: Action) -> Done:
        # Notes are for the HUD and the log. They never touch Logic.
        return Done("confirmed", f"note:{a.name}", a.reason or "")
