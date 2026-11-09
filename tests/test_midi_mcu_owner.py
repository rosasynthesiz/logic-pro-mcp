"""Only one process may publish the MCU ports.

Each extra copy of the ports reaches Logic without the fixed unique IDs, so Logic installs another Mackie
Control chained at the next bank offset. On 2026-11-09 that left three units on one port (offsets 0/8/16)
after a doctor run and a probe ran beside Hermes' own servers. These tests use fake rtmidi classes: opening
real virtual ports here would put a new device in front of a running Logic and reproduce the bug."""
from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import time

import pytest

from logic_mcp.channels import midi
from logic_mcp.channels.midi import MCU_PORT, MidiBus


class _FakePort:
    published: list[str] = []      # names other processes publish, as get_ports() would list them
    opened: list[str] = []         # virtual ports opened by this test process
    closed: list = []              # ports closed after having been opened
    open_delay = 0.0               # widen the window between "checked" and "published" for race tests
    fail_on_open: int | None = None  # raise on the Nth open_virtual_port call (1-based)

    def __init__(self) -> None:
        self.open = False

    def get_ports(self):
        return list(_FakePort.published)

    def open_virtual_port(self, name):
        time.sleep(_FakePort.open_delay)
        if _FakePort.fail_on_open == len(_FakePort.opened) + 1:
            raise RuntimeError("MIDIDestinationCreate failed")
        _FakePort.opened.append(name); self.open = True

    def close_port(self):
        if self.open:
            _FakePort.closed.append(self)
        self.open = False

    def ignore_types(self, **_):
        pass

    def set_callback(self, _cb):
        pass

    def cancel_callback(self):
        pass


@pytest.fixture(autouse=True)
def _fake_midi(monkeypatch, tmp_path):
    _FakePort.published = []
    _FakePort.opened = []
    _FakePort.closed = []
    _FakePort.open_delay = 0.0
    _FakePort.fail_on_open = None
    monkeypatch.setattr(midi.rtmidi, "MidiOut", _FakePort)
    monkeypatch.setattr(midi.rtmidi, "MidiIn", _FakePort)
    monkeypatch.setattr(midi, "MCU_LOCK_PATH", tmp_path / "mcu.lock")
    monkeypatch.setattr(MidiBus, "_pin_unique_ids", staticmethod(lambda: None))


def test_first_bus_owns_the_ports_and_records_its_pid(tmp_path):
    a = MidiBus()
    assert a.open_mcu()
    assert _FakePort.opened == [MCU_PORT, MCU_PORT]
    assert (tmp_path / "mcu.lock").read_text() == str(os.getpid())
    a.close()


def test_second_bus_leaves_mcu_alone_while_the_first_holds_it():
    a, b = MidiBus(), MidiBus()
    assert a.open_mcu()
    assert not b.open_mcu()
    assert _FakePort.opened == [MCU_PORT, MCU_PORT]           # b published nothing
    assert "owned by another logic-pro-mcp process" in b.mcu.unavailable
    assert str(os.getpid()) in b.mcu.unavailable
    t0 = time.monotonic()
    assert not b.wait_ready(timeout=5)
    assert time.monotonic() - t0 < 0.5                        # returns at once, no 5 s stall
    assert not b.mcu_fader(0, 100) and not b.mcu_strip("mute", 0) and not b.mcu_button("play")
    a.close()


def test_ports_from_a_pre_lock_server_block_publishing_and_free_the_lock():
    _FakePort.published = [MCU_PORT]                          # an older server already has them
    a = MidiBus()
    assert not a.open_mcu()
    assert _FakePort.opened == []
    assert "already published by another process" in a.mcu.unavailable
    _FakePort.published = []                                  # that server exits
    b = MidiBus()
    assert b.open_mcu()                                       # lock was not left held by `a`
    b.close()


def test_ownership_passes_on_when_the_owner_closes():
    a, b = MidiBus(), MidiBus()
    assert a.open_mcu() and not b.open_mcu()
    a.close()
    assert b.open_mcu()
    assert b.mcu.unavailable == ""
    assert "unavailable" not in b.mcu.to_dict()
    b.close()


def test_open_is_idempotent_for_the_owner():
    a = MidiBus()
    assert a.open_mcu() and a.open_mcu()
    assert _FakePort.opened == [MCU_PORT, MCU_PORT]
    a.close()


def test_parallel_tool_calls_in_one_server_publish_exactly_one_pair():
    # health + transport state on an agent's first turn run on two worker threads of the same server.
    _FakePort.open_delay = 0.02
    b = MidiBus()
    results: list[bool] = []
    threads = [threading.Thread(target=lambda: results.append(b.open_mcu())) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results == [True] * 8
    assert _FakePort.opened == [MCU_PORT, MCU_PORT]
    assert b.mcu.unavailable == ""
    assert b._mcu_lock_fd is not None                         # owner still holds its lock
    b.close()


def test_failed_port_creation_frees_the_lock_and_leaves_nothing_half_open():
    _FakePort.fail_on_open = 2                                # output opens, input raises
    a = MidiBus()
    assert not a.open_mcu()
    assert "could not publish MCU ports" in a.mcu.unavailable
    assert a._mcu_out is None and a._mcu_in is None
    assert len(_FakePort.closed) == 1                         # the output that did open was taken down again
    _FakePort.fail_on_open = None
    _FakePort.opened = []
    b = MidiBus()
    assert b.open_mcu()                                       # nobody is locked out by the broken attempt
    b.close()


def test_unwritable_lock_location_fails_closed_without_raising(monkeypatch, tmp_path):
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("")
    monkeypatch.setattr(midi, "MCU_LOCK_PATH", blocker / "mcu.lock")
    a = MidiBus()
    assert not a.open_mcu()
    assert "lock unavailable" in a.mcu.unavailable
    assert _FakePort.opened == []


def test_flock_unsupported_is_not_reported_as_another_owner(monkeypatch):
    def nope(_fd, _op):
        raise OSError(errno.ENOTSUP, "Operation not supported")
    monkeypatch.setattr(midi.fcntl, "flock", nope)
    a = MidiBus()
    assert not a.open_mcu()
    assert "lock failed" in a.mcu.unavailable and "another" not in a.mcu.unavailable


def test_lock_held_by_another_process_is_freed_when_it_dies(tmp_path):
    holder = subprocess.Popen(
        [sys.executable, "-c",
         "import fcntl, os, sys, time\n"
         f"fd = os.open({str(tmp_path / 'mcu.lock')!r}, os.O_RDWR | os.O_CREAT, 0o644)\n"
         "fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n"
         "os.pwrite(fd, str(os.getpid()).encode(), 0)\n"
         "print('locked', flush=True); time.sleep(60)\n"],
        stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "locked"
        a = MidiBus()
        assert not a.open_mcu()
        assert f"pid {holder.pid}" in a.mcu.unavailable
        holder.kill(); holder.wait(timeout=5)
        assert a.open_mcu()                                   # the kernel dropped the dead owner's lock
        a.close()
    finally:
        if holder.poll() is None:
            holder.kill(); holder.wait(timeout=5)
        holder.stdout.close()


def test_close_resets_handshake_so_a_reopened_surface_must_handshake_again():
    a = MidiBus()
    assert a.open_mcu()
    a.mcu.handshake, a.mcu.handshake_at = True, 0.0
    a.close()
    assert not a.mcu.handshake and not a.mcu.ready()
    assert a._mcu_lock_fd is None


def test_doctor_does_not_ask_for_another_surface_when_mcu_is_owned_elsewhere(monkeypatch):
    from logic_mcp import doctor
    owner, other = MidiBus(), MidiBus()
    assert owner.open_mcu()
    monkeypatch.setattr(doctor, "session_state", lambda: {"ok": True, "locked": False, "display_asleep": False})
    monkeypatch.setattr(doctor.target, "find", lambda: None)
    monkeypatch.setattr(doctor.ax, "trusted", lambda: True)
    monkeypatch.setattr(doctor.applescript, "frontmost", lambda: "Logic Pro")
    monkeypatch.setattr(doctor, "bus", lambda: other)
    report = doctor.run()
    mcu = next(c for c in report["checks"] if c["name"] == "mcu_surface")
    assert mcu["status"] == "warn" and "owned by another logic-pro-mcp process" in mcu["summary"]
    assert not any("Mackie" in f for f in report["fix_plan"])
    owner.close()


def test_failure_after_publishing_still_tears_everything_down(monkeypatch):
    def boom():
        raise RuntimeError("pin failed")
    monkeypatch.setattr(MidiBus, "_pin_unique_ids", staticmethod(boom))
    a = MidiBus()
    assert not a.open_mcu()
    assert a._mcu_out is None and a._mcu_in is None and a._mcu_lock_fd is None
    assert len(_FakePort.closed) == 2


def test_callback_after_close_is_ignored():
    a = MidiBus()
    assert a.open_mcu()
    a.close()
    a._on_mcu(([0x90, 0x5E, 0x7F], 0.0))                      # a play-LED message still in flight
    assert a.mcu.rx_count == 0 and a.mcu.leds == {}
