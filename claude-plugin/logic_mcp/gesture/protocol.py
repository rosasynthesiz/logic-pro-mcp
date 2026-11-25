"""The wire contract between the gesture processes. Nothing here imports Logic.

Four processes, three hops, all on loopback:

    handd  (Swift, camera)  --UDP 9101-->  gestured  --unix socket-->  logicpadd  --UDP 7000--> Logic
    voiced (Python, mic)    --UDP 9102-->     |
                                              +------UDP 9103------>  hud

Why UDP between the sensors and `gestured`: a hand frame that arrives late is worth less than the frame
behind it, so dropping is the correct failure mode. A TCP stream would queue stale frames and the fader
would lag further behind the hand the longer the session ran. The control hop to `logicpadd` is a unix
socket instead, because there every message must land exactly once.

Every payload is one JSON object on one line, UTF-8, newline-terminated. Unknown keys are ignored, so a
newer sensor can add fields without breaking an older `gestured`.
"""
from __future__ import annotations

import json
import socket
import time
from dataclasses import dataclass, field
from typing import Any, Iterator

LOOPBACK = "127.0.0.1"
PORT_HANDS = 9101          # handd  -> gestured
PORT_VOICE = 9102          # voiced -> gestured
PORT_HUD = 9103            # gestured -> hud
PADD_SOCKET = "/tmp/logicpadd.sock"    # gestured/logic-mcp -> logicpadd

MAX_DATAGRAM = 65507

# The 21 Vision / MediaPipe hand landmarks, in the order both emit them. Index into HandFrame.lm.
WRIST = 0
THUMB_CMC, THUMB_MCP, THUMB_IP, THUMB_TIP = 1, 2, 3, 4
INDEX_MCP, INDEX_PIP, INDEX_DIP, INDEX_TIP = 5, 6, 7, 8
MIDDLE_MCP, MIDDLE_PIP, MIDDLE_DIP, MIDDLE_TIP = 9, 10, 11, 12
RING_MCP, RING_PIP, RING_DIP, RING_TIP = 13, 14, 15, 16
PINKY_MCP, PINKY_PIP, PINKY_DIP, PINKY_TIP = 17, 18, 19, 20

FINGER_TIPS = (THUMB_TIP, INDEX_TIP, MIDDLE_TIP, RING_TIP, PINKY_TIP)
FINGER_MCPS = (THUMB_MCP, INDEX_MCP, MIDDLE_MCP, RING_MCP, PINKY_MCP)
FINGER_PIPS = (THUMB_IP, INDEX_PIP, MIDDLE_PIP, RING_PIP, PINKY_PIP)

# The six poses the classifier may report, plus the absence of one. Keep this list closed: every pose
# beyond six costs more in false fires than it returns in convenience (see GESTURE_VOICE_PLAN.md).
POSES = ("open_palm", "fist", "pinch", "point", "swipe_left", "spread")
NO_POSE = "none"

# Arbiter states.
IDLE, ARMED, ENGAGED, FROZEN = "idle", "armed", "engaged", "frozen"
STATES = (IDLE, ARMED, ENGAGED, FROZEN)

# Continuous axes a target can be bound to.
AXES = ("volume", "pan", "param", "scrub", "eq")


def now() -> float:
    """Monotonic seconds. Every timestamp on this bus is from the same clock on the same machine."""
    return time.monotonic()


# --------------------------------------------------------------------------- hand frames
@dataclass(slots=True)
class Hand:
    """One detected hand. `lm` is 21 [x, y, z] triples, normalised to the frame: x and y in 0..1 with
    the origin top-left, z relative to the wrist in the same units as x (negative = toward the camera).

    Vision reports y from the BOTTOM in its own coordinate space; `handd` flips it before sending, so
    everything downstream can assume screen order. It also mirrors x for a user-facing camera, so
    "hand moves right" always means x increases.
    """
    chirality: str                     # "left" | "right" | "unknown"
    score: float                       # 0..1 detector confidence
    lm: list[list[float]]              # 21 x [x, y, z]

    def tip(self, i: int) -> list[float]:
        return self.lm[i]

    def valid(self) -> bool:
        return len(self.lm) == 21 and all(len(p) == 3 for p in self.lm)


@dataclass(slots=True)
class HandFrame:
    """One camera frame's worth of hands."""
    t: float
    hands: list[Hand] = field(default_factory=list)
    source: str = "handd"

    @property
    def primary(self) -> Hand | None:
        """The hand we drive from: highest score, ties broken toward the right hand."""
        if not self.hands:
            return None
        return max(self.hands, key=lambda h: (h.score, h.chirality == "right"))

    def to_json(self) -> str:
        return json.dumps({"t": self.t, "source": self.source,
                           "hands": [{"chirality": h.chirality, "score": h.score, "lm": h.lm}
                                     for h in self.hands]}, separators=(",", ":"))

    @classmethod
    def from_obj(cls, o: dict) -> "HandFrame":
        hands = []
        for h in o.get("hands") or []:
            hand = Hand(chirality=str(h.get("chirality", "unknown")),
                        score=float(h.get("score", 0.0)),
                        lm=[[float(c) for c in p] for p in h.get("lm") or []])
            if hand.valid():
                hands.append(hand)
        return cls(t=float(o.get("t", now())), hands=hands, source=str(o.get("source", "handd")))


# --------------------------------------------------------------------------- voice
@dataclass(slots=True)
class Utterance:
    """One push-to-talk turn. `final` is False for interim hypotheses, which `gestured` shows on the HUD
    but never acts on — acting on an interim is how you end up muting the wrong track."""
    t: float
    text: str
    final: bool = True
    confidence: float = 1.0

    def to_json(self) -> str:
        return json.dumps({"t": self.t, "text": self.text, "final": self.final,
                           "confidence": self.confidence}, separators=(",", ":"))

    @classmethod
    def from_obj(cls, o: dict) -> "Utterance":
        return cls(t=float(o.get("t", now())), text=str(o.get("text", "")),
                   final=bool(o.get("final", True)), confidence=float(o.get("confidence", 1.0)))


# --------------------------------------------------------------------------- intents
@dataclass(slots=True)
class Intent:
    """What the grammar (or the model) decided an utterance means.

    `verb` is the action. `target` is a track name or index, resolved later — the grammar never guesses
    which track "strings" is, it just passes the word along. `value` is present only when the human said
    an explicit number, in which case the hand is not involved at all.
    """
    verb: str
    target: str | None = None
    axis: str | None = None
    value: float | None = None
    unit: str | None = None
    raw: str = ""
    source: str = "grammar"            # "grammar" | "model"
    confidence: float = 1.0

    def to_json(self) -> str:
        return json.dumps({k: v for k, v in {
            "verb": self.verb, "target": self.target, "axis": self.axis, "value": self.value,
            "unit": self.unit, "raw": self.raw, "source": self.source,
            "confidence": self.confidence}.items() if v is not None}, separators=(",", ":"))

    @classmethod
    def from_obj(cls, o: dict) -> "Intent":
        return cls(verb=str(o.get("verb", "")), target=o.get("target"), axis=o.get("axis"),
                   value=o.get("value"), unit=o.get("unit"), raw=str(o.get("raw", "")),
                   source=str(o.get("source", "grammar")), confidence=float(o.get("confidence", 1.0)))


# --------------------------------------------------------------------------- HUD
@dataclass(slots=True)
class HudState:
    """What the overlay shows. Every field here is what Logic last confirmed, never what we asked for —
    the whole point of the HUD is to show the truth when you cannot look at the screen."""
    t: float
    state: str = IDLE
    target: str | None = None
    axis: str | None = None
    value: float | None = None
    readout: str = ""
    pose: str = NO_POSE
    hands: int = 0
    note: str = ""

    def to_json(self) -> str:
        return json.dumps({"t": self.t, "state": self.state, "target": self.target, "axis": self.axis,
                           "value": self.value, "readout": self.readout, "pose": self.pose,
                           "hands": self.hands, "note": self.note}, separators=(",", ":"))


# --------------------------------------------------------------------------- transport
def sender(port: int, host: str = LOOPBACK) -> "UdpSender":
    return UdpSender(port, host)


class UdpSender:
    """Fire-and-forget line sender. Never raises on a dead receiver: a sensor must not die because the
    consumer restarted."""

    def __init__(self, port: int, host: str = LOOPBACK):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.dropped = 0

    def send(self, line: str) -> bool:
        data = line.encode("utf-8")
        if len(data) > MAX_DATAGRAM:
            self.dropped += 1
            return False
        try:
            self.sock.sendto(data, self.addr)
            return True
        except OSError:
            self.dropped += 1
            return False

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


class UdpReceiver:
    """Blocking line receiver with a timeout, so a consumer can interleave its own housekeeping.

    `drain()` returns only the newest datagram per call cycle when `latest_only` is set — for hand frames
    that is what you want, because acting on a queued 200 ms-old frame is worse than skipping it.
    """

    def __init__(self, port: int, host: str = "0.0.0.0", timeout: float = 0.05):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.timeout = timeout
        self.sock.settimeout(timeout)
        self.received = 0
        self.bad = 0

    MAX_PER_DRAIN = 256

    def drain(self, latest_only: bool = False) -> list[dict]:
        """Everything that has ALREADY arrived, right now. Never waits for more.

        This reads non-blocking rather than "until the socket goes quiet for `timeout`", and the
        difference is not academic: a 60 fps camera puts a datagram on the wire every 16 ms and so never
        goes quiet for the 50 ms a timeout-driven loop was waiting for. The loop then only returned on
        scheduler jitter, which made the whole daemon run in unpredictable bursts — poses never settled
        long enough to classify and the HUD updated at random. `MAX_PER_DRAIN` is a second guard, in
        case a sender ever outruns the consumer badly enough to keep the buffer permanently full.
        """
        out: list[dict] = []
        self.sock.settimeout(0.0)
        try:
            for _ in range(self.MAX_PER_DRAIN):
                try:
                    buf, _addr = self.sock.recvfrom(MAX_DATAGRAM)
                except (BlockingIOError, socket.timeout, TimeoutError):
                    break
                except OSError:
                    break
                try:
                    out.append(json.loads(buf.decode("utf-8")))
                    self.received += 1
                except (ValueError, UnicodeDecodeError):
                    # One malformed datagram must never take the loop down; count it and carry on.
                    self.bad += 1
        finally:
            self.sock.settimeout(self.timeout)
        if latest_only and len(out) > 1:
            return out[-1:]
        return out

    def stream(self) -> Iterator[dict]:
        """Poll forever. `drain` no longer blocks, so this sleeps rather than spinning a core."""
        while True:
            got = self.drain()
            if not got:
                time.sleep(self.timeout or 0.005)
            for o in got:
                yield o

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def jsonl(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"))
