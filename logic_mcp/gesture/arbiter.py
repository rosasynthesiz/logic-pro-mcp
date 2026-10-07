"""The arbiter: the one place that decides whether a hand movement is allowed to touch Logic.

It is a pure state machine. It performs no IO, opens no socket and imports nothing from `logic_mcp`
outside this package — feed it events, read back `Action`s, and the caller executes them. That is
deliberate: this is the component where a mistake mutes the wrong track or fires Stop mid-take, so it
has to be exhaustively testable without Logic anywhere near it.

    IDLE  --intent names a track + axis-->  ARMED
    ARMED --clutch closes--------------->  ENGAGED      (the hand now owns the axis)
    ENGAGED --clutch opens------------->  FROZEN        (value stays exactly where it was left)
    FROZEN --clutch closes------------->  ENGAGED       (re-grab the same target)
    FROZEN --4 s of nothing----------->  IDLE
    ARMED  --10 s of nothing---------->  IDLE

Three rules the machine exists to enforce:

1. **No target, no motion.** A hand waving in IDLE does nothing continuous. Naming is not touching, so
   even ARMED does nothing until the clutch closes.
2. **Discrete poses are suppressed while ENGAGED.** Fingers curl on their way through a fader ride; if
   `fist` still meant Stop there, every long ride would eventually stop the transport.
3. **Release freezes, never resets.** Letting go of the clutch is the most common thing a hand does, and
   it happens by accident. Snapping the fader to zero on an accidental release would be unforgivable, so
   the last value simply stands.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import protocol as P

# How long a binding survives with nothing happening.
FROZEN_TTL = 4.0
ARMED_TTL = 10.0

# Verbs that name a continuous binding rather than doing something immediately. Anything else is a
# one-shot command and never changes the state machine.
AXIS_VERBS = {"volume": "volume", "pan": "pan", "param": "param", "scrub": "scrub", "eq": "eq"}

# Poses that fire a command, and what they fire. `pinch` is absent on purpose — it is the clutch and
# never a command.
POSE_COMMANDS = {
    "open_palm": ("transport", "play"),
    "fist": ("transport", "stop"),
    "swipe_left": ("key", "undo"),
    "point": ("select", "under_pointer"),
    "spread": ("key", "zoom"),
}

CLUTCH_POSE = "pinch"


@dataclass(slots=True)
class Action:
    """Something the caller should actually do to Logic. `kind` picks the executor."""
    kind: str                      # "transport" | "key" | "select" | "axis" | "command" | "note"
    name: str = ""
    target: str | None = None
    axis: str | None = None
    value: float | None = None
    unit: str | None = None
    reason: str = ""

    def __repr__(self) -> str:      # keeps test failures readable
        bits = [self.kind, self.name]
        if self.target:
            bits.append(f"target={self.target}")
        if self.axis:
            bits.append(f"axis={self.axis}")
        if self.value is not None:
            bits.append(f"value={self.value:.3f}")
        return f"<Action {' '.join(b for b in bits if b)}>"


@dataclass(slots=True)
class Binding:
    """What the hand currently owns."""
    target: str
    axis: str
    value: float | None = None      # last value actually sent, 0..1
    armed_at: float = 0.0
    touched_at: float = 0.0


class Arbiter:
    def __init__(self, frozen_ttl: float = FROZEN_TTL, armed_ttl: float = ARMED_TTL):
        self.state: str = P.IDLE
        self.binding: Binding | None = None
        self.pose: str = P.NO_POSE
        self.clutch: bool = False
        self.frozen_ttl = frozen_ttl
        self.armed_ttl = armed_ttl
        self.last_change: float = 0.0
        self.pending: Intentish | None = None      # a re-target that arrived mid-ride
        self.history: list[tuple[float, str, str]] = []   # (t, from, to) — for the HUD and for tests

    # ------------------------------------------------------------------ helpers
    def _goto(self, state: str, t: float) -> None:
        if state == self.state:
            return
        self.history.append((t, self.state, state))
        if len(self.history) > 200:
            del self.history[:100]
        self.state = state
        self.last_change = t

    @property
    def engaged(self) -> bool:
        return self.state == P.ENGAGED

    @property
    def target(self) -> str | None:
        return self.binding.target if self.binding else None

    @property
    def axis(self) -> str | None:
        return self.binding.axis if self.binding else None

    # ------------------------------------------------------------------ inputs
    def on_intent(self, intent: P.Intent, t: float) -> list[Action]:
        """A parsed utterance. Either it binds an axis, or it is a one-shot command."""
        verb = intent.verb

        # An explicit number means the human already said the value; the hand is not involved and the
        # state machine is not touched. "volume vocals to minus three" must not hijack a live ride.
        if verb in AXIS_VERBS and intent.value is not None:
            return [Action("command", verb, target=intent.target, axis=AXIS_VERBS[verb],
                           value=intent.value, unit=intent.unit, reason="explicit value")]

        if verb in AXIS_VERBS:
            if not intent.target:
                return [Action("note", "no_target", reason=f"{verb} needs a track name")]
            # Re-targeting mid-ride is held back until the clutch opens: swapping the binding under a
            # closed clutch would make the hand jump a different fader to wherever it happens to be.
            if self.state == P.ENGAGED:
                self.pending = (intent.target, AXIS_VERBS[verb])
                return [Action("note", "queued", target=intent.target, axis=AXIS_VERBS[verb],
                               reason="re-target held until the clutch opens")]
            self.binding = Binding(target=intent.target, axis=AXIS_VERBS[verb], armed_at=t, touched_at=t)
            self._goto(P.ARMED, t)
            return [Action("note", "armed", target=intent.target, axis=AXIS_VERBS[verb])]

        # Everything else is a one-shot. It runs in any state — including ENGAGED, because voice is
        # deliberate in a way a pose is not.
        return [Action("command", verb, target=intent.target, axis=intent.axis,
                       value=intent.value, unit=intent.unit, reason=intent.source)]

    def on_pose(self, pose: str, t: float) -> list[Action]:
        """The debounced pose currently held (`P.NO_POSE` when the hand shows none).

        Order matters here. The clutch transition is resolved FIRST, and a command pose that arrives in
        the same transition as a clutch release is swallowed rather than fired. Without that, the
        ordinary way a ride ends — fingers uncurling out of the pinch through something the classifier
        reads for one frame as a fist — would fire Stop every single time.
        """
        out: list[Action] = []
        if pose == self.pose:
            return out
        prev, self.pose = self.pose, pose

        was, self.clutch = self.clutch, (pose == CLUTCH_POSE)
        clutch_moved = was != self.clutch

        # --- clutch closing
        if self.clutch and not was:
            if self.binding is not None and self.state in (P.ARMED, P.FROZEN):
                self.binding.touched_at = t
                self._goto(P.ENGAGED, t)
                out.append(Action("note", "engaged", target=self.binding.target, axis=self.binding.axis))
            else:
                out.append(Action("note", "clutch_no_target",
                                  reason="nothing is armed; say which track first"))
            return out

        # --- clutch opening
        if was and not self.clutch:
            if self.state == P.ENGAGED:
                self._goto(P.FROZEN, t)
                out.append(Action("note", "frozen", target=self.target, axis=self.axis,
                                  value=self.binding.value if self.binding else None))
                if self.pending is not None:
                    tgt, ax = self.pending
                    self.pending = None
                    self.binding = Binding(target=tgt, axis=ax, armed_at=t, touched_at=t)
                    self._goto(P.ARMED, t)
                    out.append(Action("note", "armed", target=tgt, axis=ax, reason="queued re-target"))

        # --- discrete commands (rule 2)
        if pose in POSE_COMMANDS and prev != pose:
            if self.state == P.ENGAGED or clutch_moved:
                out.append(Action("note", "suppressed",
                                  reason=f"{pose} ignored: poses are not commands while the clutch "
                                         f"is closed or moving"))
            else:
                kind, name = POSE_COMMANDS[pose]
                out.append(Action(kind, name, reason=f"pose {pose}"))
        return out

    def on_hands_lost(self, t: float) -> list[Action]:
        """The camera stopped seeing the hand. Treated exactly like opening the clutch — the value
        holds. A dropped frame must never read as 'the hand moved to the bottom of the box'.

        Do NOT clear `self.pose` before delegating: `on_pose` short-circuits when the pose has not
        changed, so pre-clearing it silently turned this whole method into a no-op and a lost hand
        stayed engaged.
        """
        if self.clutch or self.state == P.ENGAGED:
            return self.on_pose(P.NO_POSE, t) or [Action("note", "frozen", reason="hand lost")]
        self.pose = P.NO_POSE
        return []

    def on_value(self, v: float, t: float) -> list[Action]:
        """A normalised 0..1 reading off whichever axis is bound. Ignored unless ENGAGED (rule 1)."""
        if self.state != P.ENGAGED or self.binding is None:
            return []
        v = 0.0 if v < 0.0 else 1.0 if v > 1.0 else v
        self.binding.value = v
        self.binding.touched_at = t
        return [Action("axis", self.binding.axis, target=self.binding.target,
                       axis=self.binding.axis, value=v)]

    def tick(self, t: float) -> list[Action]:
        """Call on every loop. Expires stale bindings so the hand never wakes up owning something
        you named two minutes ago."""
        if self.state == P.FROZEN and (t - self.last_change) >= self.frozen_ttl:
            self.binding = None
            self._goto(P.IDLE, t)
            return [Action("note", "released", reason="frozen timeout")]
        if self.state == P.ARMED and (t - self.last_change) >= self.armed_ttl:
            self.binding = None
            self.pending = None
            self._goto(P.IDLE, t)
            return [Action("note", "released", reason="armed timeout")]
        return []

    # ------------------------------------------------------------------ reporting
    def snapshot(self, t: float | None = None) -> dict:
        b = self.binding
        return {"state": self.state, "target": b.target if b else None, "axis": b.axis if b else None,
                "value": b.value if b else None, "pose": self.pose, "clutch": self.clutch,
                "pending": list(self.pending) if self.pending else None,
                "age": round((t if t is not None else 0.0) - self.last_change, 3)}


Intentish = tuple           # (target, axis) — the queued re-target
