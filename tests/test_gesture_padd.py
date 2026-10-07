"""Tests for logicpadd: PaddServer, RemoteLogicPad, and the acquire() seam.

osc.LogicPad opens a real UDP socket and shells out to `dns-sd` (see tests/test_osc.py's docstring on
why that must never happen in a test). So every test here runs PaddServer against a FakePad -- something
with the same surface `send`/`press`/`state`/`snapshot`/`wait_for`/`connected`/`advertised`/`refresh`/
`forget` -- never a real osc.LogicPad, and never over anything but a real unix socket on a temp path.
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import uuid

import pytest

from logic_mcp.gesture import padd


# --------------------------------------------------------------------------- fake pad
class FakePad:
    """Records what was asked of it. Good enough to exercise PaddServer's dispatch without Logic,
    a UDP socket, or dns-sd anywhere nearby."""

    def __init__(self):
        self.state = {"/1/play": [1.0], "/1/trackname1": ["Strings"]}
        self.sent: list[tuple[str, tuple]] = []
        self.pressed: list[str] = []
        self.refreshed: list[float] = []
        self.forgot = False
        self._connected = True
        self._advertised = True

    def send(self, address, *args):
        self.sent.append((address, args))
        return True

    def press(self, address):
        self.pressed.append(address)
        return True

    def snapshot(self):
        return {"connected": self._connected, "bank": [], "feedback": len(self.sent)}

    def wait_for(self, address, timeout=1.5, since=None):
        return self.state.get(address)

    @property
    def connected(self):
        return self._connected

    @property
    def advertised(self):
        return self._advertised

    def refresh(self, settle=0.6):
        self.refreshed.append(settle)

    def forget(self):
        self.forgot = True
        return 1

    def recent(self, since=0.0, limit=200):
        return [(1.0, "/1/play", [1.0]), (2.0, "/3/trackname", ["Strings"])][-limit:]


# --------------------------------------------------------------------------- fixtures
def _short_sock_path() -> str:
    """A path short enough for AF_UNIX (sun_path is ~104 bytes on macOS) -- pytest's own tmp_path is
    nested deep enough under /private/var/folders/... to blow that limit on its own."""
    return os.path.join(tempfile.gettempdir(), f"padd-{uuid.uuid4().hex[:10]}.sock")


@pytest.fixture
def sock_path():
    path = _short_sock_path()
    yield path
    try:
        os.unlink(path)
    except OSError:
        pass


@pytest.fixture
def server(sock_path):
    pad = FakePad()
    srv = padd.PaddServer(pad, socket_path=sock_path)
    srv.start()
    yield srv, pad
    srv.stop()


def raw_client(sock_path: str, timeout: float = 2.0) -> socket.socket:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    s.connect(sock_path)
    return s


def raw_call(s: socket.socket, obj: dict) -> dict:
    s.sendall((json.dumps(obj) + "\n").encode("utf-8"))
    f = s.makefile("rb")
    line = f.readline()
    return json.loads(line.decode("utf-8"))


# --------------------------------------------------------------------------- protocol round trip
def test_ping_round_trip_over_real_unix_socket(server, sock_path):
    s = raw_client(sock_path)
    resp = raw_call(s, {"op": "ping"})
    assert resp == {"ok": True, "pong": True}
    s.close()


def test_malformed_json_gets_error_response_not_a_dead_connection(server, sock_path):
    s = raw_client(sock_path)
    s.sendall(b"not json at all\n")
    f = s.makefile("rb")
    resp = json.loads(f.readline().decode("utf-8"))
    assert resp["ok"] is False
    assert "error" in resp
    # the connection must still be alive for a subsequent, well-formed request
    resp2 = raw_call(s, {"op": "ping"})
    assert resp2 == {"ok": True, "pong": True}
    s.close()


def test_non_object_json_is_rejected_not_fatal(server, sock_path):
    s = raw_client(sock_path)
    s.sendall(b"[1, 2, 3]\n")
    f = s.makefile("rb")
    resp = json.loads(f.readline().decode("utf-8"))
    assert resp["ok"] is False
    s.close()


def test_unknown_op_is_an_error(server, sock_path):
    s = raw_client(sock_path)
    resp = raw_call(s, {"op": "levitate"})
    assert resp["ok"] is False
    s.close()


def test_missing_required_field_is_an_error_not_a_crash(server, sock_path):
    s = raw_client(sock_path)
    resp = raw_call(s, {"op": "send"})     # no "address"
    assert resp["ok"] is False
    s.close()


def test_forget_is_rejected_over_the_socket(server, sock_path):
    _srv, pad = server
    s = raw_client(sock_path)
    resp = raw_call(s, {"op": "forget"})
    assert resp["ok"] is False
    assert "error" in resp
    assert pad.forgot is False, "forget() must never actually run against the pad"
    s.close()


# --------------------------------------------------------------------------- every operation, via RemoteLogicPad
def test_send(server, sock_path):
    _srv, pad = server
    client = padd.RemoteLogicPad(socket_path=sock_path)
    assert client.send("/1/play", 1.0) is True
    assert pad.sent == [("/1/play", (1.0,))]
    client.close()


def test_press(server, sock_path):
    _srv, pad = server
    client = padd.RemoteLogicPad(socket_path=sock_path)
    assert client.press("/1/bank+") is True
    assert pad.pressed == ["/1/bank+"]
    client.close()


def test_state_whole_dict_and_single_address(server, sock_path):
    client = padd.RemoteLogicPad(socket_path=sock_path)
    assert client.state() == {"/1/play": [1.0], "/1/trackname1": ["Strings"]}
    assert client.state("/1/trackname1") == ["Strings"]
    assert client.state("/nowhere") is None
    client.close()


def test_snapshot(server, sock_path):
    client = padd.RemoteLogicPad(socket_path=sock_path)
    snap = client.snapshot()
    assert snap["connected"] is True
    assert "bank" in snap
    client.close()


def test_wait_for(server, sock_path):
    client = padd.RemoteLogicPad(socket_path=sock_path)
    assert client.wait_for("/1/play", timeout=0.5) == [1.0]
    assert client.wait_for("/nowhere", timeout=0.3) is None
    client.close()


def test_connected_and_advertised(server, sock_path):
    _srv, pad = server
    client = padd.RemoteLogicPad(socket_path=sock_path)
    assert client.connected is True
    assert client.advertised is True
    pad._connected = False
    assert client.connected is False
    client.close()


def test_refresh(server, sock_path):
    _srv, pad = server
    client = padd.RemoteLogicPad(socket_path=sock_path)
    client.refresh(settle=0.01)
    assert pad.refreshed == [0.01]
    client.close()


def test_status(server, sock_path):
    client = padd.RemoteLogicPad(socket_path=sock_path)
    status = client.status()
    assert status["connected"] is True
    assert status["socket"] == sock_path
    assert isinstance(status["pid"], int)
    client.close()


def test_ping_method(server, sock_path):
    client = padd.RemoteLogicPad(socket_path=sock_path)
    assert client.ping() is True
    client.close()


def test_remote_pad_has_no_forget_method():
    """forget() must not even be reachable through the client's public surface."""
    assert not hasattr(padd.RemoteLogicPad, "forget")


# --------------------------------------------------------------------------- reconnection
def test_client_survives_daemon_restart(sock_path):
    pad1 = FakePad()
    srv1 = padd.PaddServer(pad1, socket_path=sock_path)
    srv1.start()

    client = padd.RemoteLogicPad(socket_path=sock_path)
    assert client.ping() is True

    srv1.stop()                              # closes the listening socket AND active connections

    pad2 = FakePad()
    srv2 = padd.PaddServer(pad2, socket_path=sock_path)
    srv2.start()
    try:
        # The old connection is dead; the client must notice, reconnect, and succeed against srv2.
        deadline = time.time() + 3.0
        ok = False
        while time.time() < deadline:
            if client.ping():
                ok = True
                break
            time.sleep(0.05)
        assert ok, "RemoteLogicPad did not reconnect after the daemon restarted"
        assert client.press("/1/play") is True
        assert pad2.pressed == ["/1/play"]
        assert pad1.pressed == []            # went to the NEW daemon, not the old one
    finally:
        srv2.stop()
        client.close()


# --------------------------------------------------------------------------- acquire()
def test_acquire_falls_back_to_direct_when_no_daemon(monkeypatch):
    """No daemon listening at all -> acquire() must behave exactly like constructing osc.LogicPad
    directly (never worse than before this module existed). osc.LogicPad itself is stubbed out --
    instantiating the real one opens a socket and shells out to dns-sd."""
    nowhere = _short_sock_path()      # nothing ever binds this path

    built = {}

    class DummyDirectPad:
        def __init__(self, **kwargs):
            built.update(kwargs)

    monkeypatch.setattr(padd.osc, "LogicPad", DummyDirectPad)

    pad, how = padd.acquire(prefer_daemon=True, socket_path=nowhere, port=12345)
    assert how == "direct"
    assert isinstance(pad, DummyDirectPad)
    assert built == {"port": 12345}


def test_acquire_prefers_a_live_daemon(server, sock_path, monkeypatch):
    class ExplodingDirectPad:
        def __init__(self, **kwargs):
            raise AssertionError("acquire() must not construct a direct pad when a daemon answered ping")

    monkeypatch.setattr(padd.osc, "LogicPad", ExplodingDirectPad)

    pad, how = padd.acquire(prefer_daemon=True, socket_path=sock_path)
    assert how == "daemon"
    assert isinstance(pad, padd.RemoteLogicPad)
    assert pad.ping() is True
    pad.close()


# --------------------------------------------------------------------------- concurrency
def test_concurrent_clients(server, sock_path):
    _srv, pad = server
    n_clients, n_calls = 8, 25
    errors: list[Exception] = []
    lock = threading.Lock()

    def worker(i: int):
        try:
            client = padd.RemoteLogicPad(socket_path=sock_path)
            for j in range(n_calls):
                ok = client.press(f"/1/worker{i}")
                assert ok is True
            client.close()
        except Exception as e:                 # pragma: no cover - surfaced via `errors`
            with lock:
                errors.append(e)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n_clients)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors, f"concurrent client errors: {errors}"
    assert len(pad.pressed) == n_clients * n_calls
    for i in range(n_clients):
        assert pad.pressed.count(f"/1/worker{i}") == n_calls


# --------------------------------------------------------------------------- parity with osc.LogicPad
SERVER_CALLS = ["send", "press", "state", "snapshot", "wait_for", "recent", "refresh", "page", "ensure_page",
                "full_refresh", "strip_view", "eq_view", "eq_address", "set_norm", "set_target",
                "param_address", "insert_address", "close"]


def test_remote_pad_covers_everything_the_server_calls():
    """server._osc_ready hands either pad to the same tool code. Found live 2026-10-06: with logicpadd
    running, every logic_osc action raised AttributeError on ensure_page -- the client mirrored only the
    primitives, and nothing checked the rest until Logic was on the other end."""
    missing = [n for n in SERVER_CALLS if not hasattr(padd.RemoteLogicPad, n)]
    assert missing == []


def test_page_views_run_through_the_daemon(server, sock_path):
    srv, pad = server
    pad.state.update({"/3/track#": ["7"], "/3/trackname": ["Strings"], "/3/pluginname": ["Channel EQ"],
                      "/3/parname1": ["Gain"], "/3/par1": [0.5], "/3/value1": ["0.0 dB"]})
    client = padd.RemoteLogicPad(socket_path=sock_path)
    assert client.ensure_page(3, settle=0.01) is True        # a page this client never asserted: real switch
    assert pad.sent[-1] == ("/3", ())
    assert client.ensure_page(3, settle=0.01) is False       # remembered per client
    view = client.strip_view()
    assert view["track"] == "Strings" and view["plugin"] == "Channel EQ"
    assert view["params"] == [{"n": 1, "name": "Gain", "norm": 0.5, "value": "0.0 dB"}]
    client.close()


def test_recent_and_status_address_fields(server, sock_path):
    srv, pad = server
    client = padd.RemoteLogicPad(socket_path=sock_path)
    assert client.recent(limit=1) == [(2.0, "/3/trackname", ["Strings"])]
    st = client.status()
    assert st["ip"] is None and st["ip_stale"] is False      # FakePad has no address; a LogicPad reports both
    client.close()
