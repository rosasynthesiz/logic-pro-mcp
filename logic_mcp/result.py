"""Honest result envelope. Every mutating op returns one of three states; agents must only trust CONFIRMED."""
from __future__ import annotations

from typing import Any

CONFIRMED, UNCERTAIN, FAILED = "confirmed", "uncertain", "failed"


def ok(channel: str, detail: str = "", readback: Any = None, **extra) -> dict:
    return {"state": CONFIRMED, "channel": channel, "detail": detail, "readback": readback, **extra}


def unsure(channel: str, reason: str, readback: Any = None, **extra) -> dict:
    return {"state": UNCERTAIN, "channel": channel, "reason": reason, "readback": readback, **extra}


def sent(channel: str, what: str, readback: Any = None, **extra) -> dict:
    """A message left this process and nothing came back to prove its effect.

    Still UNCERTAIN -- a raw MIDI note, an MCU strip button, a menu press or a quit request is not
    confirmed by the send succeeding -- but `accepted: true` lets a caller tell "sent, unverified"
    from "sent, and the readback contradicts it". Never retry an accepted write through another
    channel on the strength of this result: read the state first.
    """
    return {"state": UNCERTAIN, "channel": channel, "reason": f"{what}: sent, no readback proves the effect",
            "readback": readback, "accepted": True, **extra}


def fail(channel: str, reason: str, fix: str | None = None, **extra) -> dict:
    return {"state": FAILED, "channel": channel, "reason": reason, "fix": fix, **extra}
