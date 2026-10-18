"""Server-side policy: argument validation and mutation serialisation.

Nothing here talks to Logic. The validator rejects what a small model gets wrong most often (unknown
keys, booleans where numbers go, NaN/inf, out-of-range values, blank names) before any channel is
touched; the guard makes sure two tool calls do not drive Logic at the same time.

What the guard can and cannot promise
-------------------------------------
* In-process: an RLock. The MCP SDK runs synchronous tools on worker threads, so two concurrent
  `tools/call` requests to one server would otherwise interleave their MCU/AX/key sends. Re-entrant,
  because dispatchers call each other (delete selects first, deliver bounces first).
* Cross-process: an advisory `flock` on one file per user. Every logic-pro-mcp process on this Mac
  (one per Claude session; eleven were alive at once when measured) that takes the guard waits for
  the others, up to a deadline, then refuses rather than queueing behind unknown work.
* It cannot stop a human at the keyboard, the gesture daemon, or any software that does not take the
  lock. The fresh readback after a write is what catches those, not the lock. Do not describe it as
  ownership of Logic.
"""
from __future__ import annotations

import contextlib
import fcntl
import functools
import math
import os
import tempfile
import threading
import time
from typing import Any, Callable

from .result import fail


class Busy(RuntimeError):
    """Another mutation holds the guard and did not release it within the deadline."""


class MutationGuard:
    def __init__(self, lock_path: str | None = None, timeout: float = 10.0) -> None:
        self.lock_path = lock_path or os.environ.get("LOGIC_MCP_LOCK") or os.path.join(
            tempfile.gettempdir(), "logic-pro-mcp-mutation.lock")
        self.timeout = timeout
        self._rlock = threading.RLock()
        self._depth = 0
        self._fd: int | None = None
        self.holder: str | None = None       # what is running under the guard right now (diagnostics)

    @contextlib.contextmanager
    def held(self, label: str = "mutation", timeout: float | None = None):
        deadline = time.monotonic() + (self.timeout if timeout is None else timeout)
        if not self._rlock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise Busy(f"{self.holder or 'another call'} is still running in this process")
        try:
            if self._depth == 0:
                self._flock(deadline, label)          # Busy propagates; the outer finally releases the RLock
                self.holder = label
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
                if self._depth == 0:
                    self._unflock()
                    self.holder = None
        finally:
            self._rlock.release()

    def _flock(self, deadline: float, label: str) -> None:
        try:
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            self._fd = None
            return                        # no lock file possible: in-process serialisation only
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise Busy(f"another logic-pro-mcp process holds {self.lock_path}")
                time.sleep(0.05)
        self._fd = fd
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()} {label}\n".encode())
        except OSError:
            pass

    def _unflock(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


guard = MutationGuard()


def serialized(read_actions: frozenset[str] | set[str] = frozenset(), channel: str = "policy"):
    """Run a dispatcher tool under the mutation guard, except for its read-only actions.

    A call that cannot take the guard in time reports `failed` with the reason instead of waiting
    behind an unknown amount of work: a model that blocks forever learns nothing and retries blind.
    """
    reads = frozenset(read_actions)

    def deco(fn: Callable[..., dict]) -> Callable[..., dict]:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            action = kwargs.get("action", args[0] if args else None)
            if action in reads:
                return fn(*args, **kwargs)
            try:
                with guard.held(f"{fn.__name__}:{action}"):
                    return fn(*args, **kwargs)
            except Busy as e:
                return fail(channel, f"refusing {fn.__name__}({action}): {e}",
                            "wait for the running call to finish, then read the state before retrying")
        return wrapper
    return deco


# ---------------------------------------------------------------- numbers the dispatchers accept
def finite(x: Any, lo: float | None = None, hi: float | None = None) -> bool:
    """A real, finite number inside [lo, hi]. Booleans are not numbers here: `True` as a tempo is a bug."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    if not math.isfinite(x):
        return False
    return (lo is None or x >= lo) and (hi is None or x <= hi)


# ---------------------------------------------------------------- compact-profile argument validation
def validate(args: Any, schema: dict) -> dict:
    """Check `args` against a small JSON-schema subset and return a clean copy, or raise ValueError.

    Covers exactly what the compact profile's schemas use: an object with properties/required, no
    extra keys, integer/number/boolean/string, minimum/maximum, enum, minLength/maxLength. Booleans
    are never numbers, numbers must be finite, a required key must be present AND non-null, and an
    unknown key is an error -- a model-invented `approved: true` must fail loudly, not be ignored.
    """
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ValueError("arguments must be an object")
    props = schema.get("properties", {})
    unknown = sorted(set(args) - set(props))
    if unknown:
        raise ValueError(f"unknown argument(s) {unknown}; allowed: {sorted(props)}")
    missing = [k for k in schema.get("required", []) if args.get(k) is None]
    if missing:
        raise ValueError(f"missing required argument(s) {missing}")
    out: dict = {}
    for key, val in args.items():
        if val is None:
            continue
        out[key] = _check(key, val, props[key])
    return out


def _check(key: str, val: Any, spec: dict) -> Any:
    typ = spec.get("type")
    if typ == "boolean":
        if not isinstance(val, bool):
            raise ValueError(f"{key} must be true or false")
        return val
    if typ in ("integer", "number"):
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError(f"{key} must be a {typ}")
        if not math.isfinite(val):
            raise ValueError(f"{key} must be a finite number")
        if typ == "integer":
            if isinstance(val, float):
                if not val.is_integer():
                    raise ValueError(f"{key} must be an integer")
                val = int(val)
        lo, hi = spec.get("minimum"), spec.get("maximum")
        if lo is not None and val < lo:
            raise ValueError(f"{key} must be >= {lo}")
        if hi is not None and val > hi:
            raise ValueError(f"{key} must be <= {hi}")
        return val
    if typ == "string":
        if not isinstance(val, str):
            raise ValueError(f"{key} must be a string")
        if "maxLength" in spec and len(val) > spec["maxLength"]:
            raise ValueError(f"{key} is longer than {spec['maxLength']} characters")
        if spec.get("minLength") and len(val.strip()) < spec["minLength"]:
            raise ValueError(f"{key} must not be blank")
        if "enum" in spec and val not in spec["enum"]:
            raise ValueError(f"{key} must be one of {spec['enum']}")
        return val
    raise ValueError(f"{key}: unsupported schema type {typ!r}")
