"""Argument validation and the mutation guard, with no Logic anywhere near."""
from __future__ import annotations

import subprocess
import sys
import threading

import pytest

from logic_mcp import policy

SCHEMA = {"type": "object", "additionalProperties": False,
          "properties": {"bpm": {"type": "number", "minimum": 5, "maximum": 990},
                         "idx": {"type": "integer", "minimum": 1},
                         "name": {"type": "string", "minLength": 1, "maxLength": 8},
                         "on": {"type": "boolean"},
                         "mode": {"type": "string", "enum": ["a", "b"]}},
          "required": ["bpm"]}


def test_clean_args_pass_through():
    assert policy.validate({"bpm": 120, "idx": 3.0, "name": "Vox", "on": True, "mode": "a"}, SCHEMA) == \
        {"bpm": 120, "idx": 3, "name": "Vox", "on": True, "mode": "a"}


@pytest.mark.parametrize("args,msg", [
    ({"bpm": 120, "approved": True}, "unknown argument"),
    ({}, "missing required"),
    ({"bpm": None}, "missing required"),
    ({"bpm": "120"}, "bpm must be a number"),
    ({"bpm": True}, "bpm must be a number"),
    ({"bpm": float("nan")}, "finite"),
    ({"bpm": float("inf")}, "finite"),
    ({"bpm": 4}, ">= 5"),
    ({"bpm": 991}, "<= 990"),
    ({"bpm": 120, "idx": 0}, ">= 1"),
    ({"bpm": 120, "idx": 2.5}, "must be an integer"),
    ({"bpm": 120, "idx": "2"}, "must be a integer"),
    ({"bpm": 120, "name": "   "}, "not be blank"),
    ({"bpm": 120, "name": "longer than"}, "longer than 8"),
    ({"bpm": 120, "name": 5}, "must be a string"),
    ({"bpm": 120, "on": 1}, "true or false"),
    ({"bpm": 120, "mode": "c"}, "one of"),
    ("not an object", "must be an object"),
])
def test_bad_args_are_refused(args, msg):
    with pytest.raises(ValueError, match=msg):
        policy.validate(args, SCHEMA)


def test_finite_helper():
    assert policy.finite(1.5, 0, 2) and policy.finite(0, 0, 0)
    for bad in (True, False, None, "1", float("nan"), float("inf"), -float("inf")):
        assert not policy.finite(bad)
    assert not policy.finite(3, 0, 2) and not policy.finite(-1, 0)


# ---------------------------------------------------------------- the guard
def test_guard_is_reentrant_in_one_thread(tmp_path):
    g = policy.MutationGuard(lock_path=str(tmp_path / "l"), timeout=0.2)
    with g.held("outer"):
        with g.held("inner"):
            assert g.holder == "outer"
    assert g.holder is None and g._fd is None


def test_second_thread_is_refused_while_first_holds(tmp_path):
    g = policy.MutationGuard(lock_path=str(tmp_path / "l"), timeout=0.2)
    started, release = threading.Event(), threading.Event()

    def holder():
        with g.held("first"):
            started.set()
            release.wait(5)
    t = threading.Thread(target=holder, daemon=True); t.start()
    assert started.wait(5)
    with pytest.raises(policy.Busy, match="first"):
        with g.held("second"):
            pass
    release.set(); t.join(5)
    with g.held("third"):
        pass


def test_another_process_holding_the_file_is_refused(tmp_path):
    lock = tmp_path / "l"
    code = ("import fcntl,sys,time; f=open(sys.argv[1],'w'); fcntl.flock(f, fcntl.LOCK_EX); "
            "print('held', flush=True); time.sleep(float(sys.argv[2]))")
    p = subprocess.Popen([sys.executable, "-c", code, str(lock), "10"], stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "held"
        g = policy.MutationGuard(lock_path=str(lock), timeout=0.3)
        with pytest.raises(policy.Busy, match="another logic-pro-mcp process"):
            with g.held("x"):
                pass
    finally:
        p.kill(); p.wait()
    with g.held("x"):
        pass


def test_guard_survives_an_unwritable_lock_path(tmp_path):
    g = policy.MutationGuard(lock_path=str(tmp_path / "no" / "such" / "dir" / "l"), timeout=0.2)
    with g.held("x"):
        assert g._fd is None                         # in-process serialisation only, and it says nothing else


def test_serialized_skips_reads_and_reports_busy(tmp_path, monkeypatch):
    g = policy.MutationGuard(lock_path=str(tmp_path / "l"), timeout=0.1)
    monkeypatch.setattr(policy, "guard", g)
    seen = []

    @policy.serialized(read_actions={"state"})
    def tool(action, x=1):
        seen.append(action)
        return {"state": "confirmed", "x": x}

    started, release = threading.Event(), threading.Event()

    def holder():
        with g.held("long write"):
            started.set(); release.wait(5)
    t = threading.Thread(target=holder, daemon=True); t.start(); assert started.wait(5)
    assert tool("state")["state"] == "confirmed"                 # reads are never blocked
    r = tool("play", x=2)
    assert r["state"] == "failed" and "long write" in r["reason"] and "play" not in seen
    release.set(); t.join(5)
    assert tool(action="play", x=3) == {"state": "confirmed", "x": 3}
    assert tool.__name__ == "tool"
