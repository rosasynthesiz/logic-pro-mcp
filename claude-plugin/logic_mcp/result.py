"""Honest result envelope. Every mutating op returns one of three states; agents must only trust CONFIRMED."""
from __future__ import annotations

from typing import Any

CONFIRMED, UNCERTAIN, FAILED = "confirmed", "uncertain", "failed"


def ok(channel: str, detail: str = "", readback: Any = None, **extra) -> dict:
    return {"state": CONFIRMED, "channel": channel, "detail": detail, "readback": readback, **extra}


def unsure(channel: str, reason: str, readback: Any = None, **extra) -> dict:
    return {"state": UNCERTAIN, "channel": channel, "reason": reason, "readback": readback, **extra}


def fail(channel: str, reason: str, fix: str | None = None, **extra) -> dict:
    return {"state": FAILED, "channel": channel, "reason": reason, "fix": fix, **extra}
