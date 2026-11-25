"""logicpadd: the one process that owns the LogicPad OSC surface, served to many clients.

`osc.LogicPad` binds UDP port 9000 and holds a `dns-sd` Bonjour advert so Logic installs it as a TouchOSC
control surface (see channels/osc.py's module docstring). Only one process can hold that port and that
advert -- and the docstring records a measured fact: Logic segfaults (SIGSEGV, 12.3) when the Bonjour
service of a bound OSC device disappears.

Two processes now want the surface: the MCP server and the real-time gesture daemon (`gestured`). This
module is the fix -- a daemon (`PaddServer`) that owns the single `LogicPad` and serves it to both over a
unix socket, plus a client (`RemoteLogicPad`) that looks enough like `LogicPad` that callers can hold
either one without caring which, and `acquire()`, the seam that picks between them.

Wire protocol: one JSON object per line in, one JSON object per line out, multiple concurrent clients
each on their own thread. Every response carries {"ok": bool, ...}; a failure carries {"ok": false,
"error": "..."} and never raises across the socket.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import sys
import threading
import time

from ..channels import osc
from . import protocol as P


# --------------------------------------------------------------------------- server-side errors
class PaddAlreadyRunning(Exception):
    """Something already answers `ping` at this socket path -- do not steal the port from it."""


# --------------------------------------------------------------------------- socket setup
def _ping_existing(path: str, timeout: float = 0.5) -> bool:
    """True if a live logicpadd answers `ping` at `path`. False for anything else -- refused, timed
    out, garbage back -- which is exactly the "stale socket file" case that is safe to unlink."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((P.jsonl({"op": "ping"}) + "\n").encode("utf-8"))
        f = s.makefile("rb")
        line = f.readline()
        s.close()
        if not line:
            return False
        resp = json.loads(line.decode("utf-8"))
        return bool(resp.get("ok")) and resp.get("pong") is True
    except (OSError, ValueError):
        return False


def _bind_unix_socket(path: str) -> socket.socket:
    """Bind `path`, unlinking it first only when it is stale (nothing answers `ping` there).

    Something DOES answer -> raise rather than steal the port from a running daemon.
    """
    if os.path.exists(path):
        if _ping_existing(path):
            raise PaddAlreadyRunning(f"a logicpadd is already listening on {path}; not starting a second one")
        os.unlink(path)                    # stale file: the process that made it is gone
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(path)
    return sock


# --------------------------------------------------------------------------- the daemon
class PaddServer:
    """Owns one `osc.LogicPad` (or, in tests, anything with the same surface) and serves it over a unix
    domain socket at `socket_path`. Each client connection gets its own thread; every call into the pad
    is serialised with `self._lock` -- the pad's own internal lock only protects its pump thread, not
    concurrent callers reaching in from here.
    """

    def __init__(self, pad, socket_path: str = P.PADD_SOCKET):
        self.pad = pad
        self.socket_path = socket_path
        self._lock = threading.Lock()
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._accept_thread: threading.Thread | None = None
        self._conns_lock = threading.Lock()
        self._conns: list[socket.socket] = []

    # ---- lifecycle
    def start(self) -> None:
        self._sock = _bind_unix_socket(self.socket_path)
        self._sock.listen(16)
        self._sock.settimeout(0.5)
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        """Close the socket and unlink the path, but leave the Bonjour advert running.

        Same segfault reason as advertise()/forget() in osc.py: withdrawing the advert while Logic is
        bound to it kills Logic. A future logicpadd adopts the still-running advert instead of
        re-registering it.
        """
        self._stop.set()
        with self._conns_lock:
            conns, self._conns = self._conns, []
        for c in conns:
            # shutdown(), not just close(): `_handle_client`'s makefile() holds its own reference to
            # the same fd, so a plain close() here leaves that fd open and the handler thread happily
            # serving requests forever. shutdown() forces the fd itself closed for I/O regardless of
            # who else still references it, which is what makes a stale client actually see EOF.
            try:
                c.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                c.close()
            except OSError:
                pass
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        try:
            if os.path.exists(self.socket_path):
                os.unlink(self.socket_path)
        except OSError:
            pass
        if self._accept_thread is not None:
            self._accept_thread.join(timeout=2.0)

    # ---- accept / serve
    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._conns_lock:
                self._conns.append(conn)
            t = threading.Thread(target=self._handle_client, args=(conn,), daemon=True)
            t.start()

    def _handle_client(self, conn: socket.socket) -> None:
        f = conn.makefile("rwb")
        try:
            for raw in f:
                line = raw.strip()
                if not line:
                    continue
                try:
                    req = json.loads(line.decode("utf-8"))
                    if not isinstance(req, dict):
                        raise ValueError("request must be a JSON object")
                except (ValueError, UnicodeDecodeError) as e:
                    resp = {"ok": False, "error": f"invalid JSON: {e}"}
                else:
                    resp = self._dispatch(req)
                try:
                    payload = P.jsonl(resp)
                except TypeError as e:
                    # A response holding something json can't serialise (e.g. a raw OSC blob) must
                    # still get an answer back, not silence the connection.
                    payload = P.jsonl({"ok": False, "error": f"unserialisable response: {e}"})
                try:
                    f.write((payload + "\n").encode("utf-8"))
                    f.flush()
                except OSError:
                    break
        except OSError:
            pass
        finally:
            with self._conns_lock:
                if conn in self._conns:
                    self._conns.remove(conn)
            try:
                conn.close()
            except OSError:
                pass

    # ---- operations
    def _dispatch(self, req: dict) -> dict:
        op = req.get("op")
        try:
            if op == "ping":
                return {"ok": True, "pong": True}

            if op == "forget":
                # NEVER expose forget() over the socket: withdrawing the Bonjour advert while Logic is
                # bound to it is the measured SIGSEGV (12.3, see osc.LogicPad.advertise()). Refuse it
                # unconditionally rather than trust a caller's judgement about whether Logic is up.
                return {"ok": False, "error": "forget() is not exposed by logicpadd: withdrawing the "
                                               "Bonjour advert while Logic is bound to it segfaults Logic"}

            if op == "send":
                address = req["address"]
                args = req.get("args") or []
                with self._lock:
                    result = self.pad.send(address, *args)
                return {"ok": True, "result": result}

            if op == "press":
                address = req["address"]
                with self._lock:
                    result = self.pad.press(address)
                return {"ok": True, "result": result}

            if op == "state":
                address = req.get("address")
                with self._lock:
                    state = self.pad.state
                    result = dict(state) if address is None else state.get(address)
                return {"ok": True, "result": result}

            if op == "snapshot":
                with self._lock:
                    result = self.pad.snapshot()
                return {"ok": True, "result": result}

            if op == "wait_for":
                # Deliberately NOT under self._lock: this can block up to `timeout`, and the pad's own
                # lock already guards the log it reads from. Holding the server lock here would stall
                # every other client's send/press for the whole wait -- fatal at 60 msg/s.
                address = req["address"]
                timeout = float(req.get("timeout", 1.5))
                since = req.get("since")
                result = self.pad.wait_for(address, timeout=timeout, since=since)
                return {"ok": True, "result": result}

            if op == "connected":
                with self._lock:
                    result = bool(self.pad.connected)
                return {"ok": True, "result": result}

            if op == "advertised":
                with self._lock:
                    result = bool(self.pad.advertised)
                return {"ok": True, "result": result}

            if op == "refresh":
                settle = float(req.get("settle", 0.6))
                with self._lock:
                    self.pad.refresh(settle=settle)
                return {"ok": True, "result": None}

            if op == "recent":
                since = float(req.get("since", 0.0))
                limit = int(req.get("limit", 200))
                result = [list(e) for e in self.pad.recent(since=since, limit=limit)]
                return {"ok": True, "result": result}

            if op == "status":
                with self._lock:
                    result = {"connected": bool(self.pad.connected), "advertised": bool(self.pad.advertised),
                              "ip": getattr(self.pad, "ip", None),
                              "ip_stale": bool(getattr(self.pad, "ip_stale", False)),
                              "socket": self.socket_path, "pid": os.getpid()}
                return {"ok": True, "result": result}

            return {"ok": False, "error": f"unknown op {op!r}"}
        except KeyError as e:
            return {"ok": False, "error": f"missing field {e}"}
        except Exception as e:      # a bad request must never take the connection or the daemon down
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}


# --------------------------------------------------------------------------- the client
class PaddUnavailable(Exception):
    """No logicpadd daemon could be reached at the configured socket."""


class RemoteLogicPad(osc._Pages):
    """Client for PaddServer, duck-typed to the same public surface as `osc.LogicPad`
    (`send`, `press`, `state`, `snapshot`, `wait_for`, `recent`, `connected`, `advertised`, `refresh`)
    so existing code can hold either one without knowing which. The page views and closed-loop setters
    (`ensure_page`, `strip_view`, `eq_view`, `set_target`, ...) come from the same `_Pages` mixin
    LogicPad uses; they only need the primitives above plus `_state_get`, which here asks the daemon.
    Found the hard way: without them every `logic_osc` call raised AttributeError the moment logicpadd
    was running -- the one configuration this module exists for.

    Holds one persistent connection and reuses it -- one write, one read, per call -- so per-call
    overhead stays low at gesture rates (up to 60 msg/s). Reconnects automatically if the daemon
    restarts; raises `PaddUnavailable` if no daemon can be reached at all.
    """

    def __init__(self, socket_path: str = P.PADD_SOCKET, timeout: float = 2.0):
        self.socket_path = socket_path
        self.timeout = timeout
        self._sock: socket.socket | None = None
        self._file = None
        self._lock = threading.Lock()
        self._page = None                 # _Pages: which page this client last asserted, None = unknown

    # ---- connection management
    def _connect(self) -> None:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect(self.socket_path)
        except OSError as e:
            sock.close()
            raise PaddUnavailable(f"no logicpadd daemon at {self.socket_path}: {e}") from e
        self._sock = sock
        self._file = sock.makefile("rwb")

    def _disconnect(self) -> None:
        try:
            if self._file is not None:
                self._file.close()
        except OSError:
            pass
        try:
            if self._sock is not None:
                self._sock.close()
        except OSError:
            pass
        self._sock = None
        self._file = None

    def _call(self, op: str, read_timeout: float | None = None, **kwargs) -> dict:
        req = {"op": op, **kwargs}
        line = (P.jsonl(req) + "\n").encode("utf-8")
        last_err: Exception | None = None
        with self._lock:
            for attempt in (1, 2):                      # one retry: the daemon may have just restarted
                try:
                    if self._sock is None:
                        self._connect()
                    self._sock.settimeout(read_timeout if read_timeout is not None else self.timeout)
                    self._file.write(line)
                    self._file.flush()
                    resp_line = self._file.readline()
                    if not resp_line:
                        raise OSError("logicpadd closed the connection")
                    return json.loads(resp_line.decode("utf-8"))
                except (OSError, ValueError) as e:
                    last_err = e
                    self._disconnect()
            raise PaddUnavailable(f"logicpadd unreachable at {self.socket_path}: {last_err}")

    @staticmethod
    def _unwrap(resp: dict, what: str):
        if not resp.get("ok"):
            raise PaddUnavailable(resp.get("error", f"{what} failed"))
        return resp.get("result")

    # ---- public surface, matching osc.LogicPad
    def send(self, address: str, *args) -> bool:
        resp = self._call("send", address=address, args=list(args))
        return bool(self._unwrap(resp, "send"))

    def press(self, address: str) -> bool:
        resp = self._call("press", address=address)
        return bool(self._unwrap(resp, "press"))

    def state(self, address: str | None = None):
        """The echo cache. With no address, the whole dict; with one, that address's last args (or None)."""
        resp = self._call("state", address=address)
        return self._unwrap(resp, "state")

    def snapshot(self) -> dict:
        resp = self._call("snapshot")
        return self._unwrap(resp, "snapshot")

    def wait_for(self, address: str, timeout: float = 1.5, since: float | None = None):
        # The read must outlive the daemon-side wait, plus headroom for the round trip itself.
        resp = self._call("wait_for", address=address, timeout=timeout, since=since,
                           read_timeout=timeout + self.timeout)
        return self._unwrap(resp, "wait_for")

    @property
    def connected(self) -> bool:
        resp = self._call("connected")
        return bool(self._unwrap(resp, "connected"))

    @property
    def advertised(self) -> bool:
        resp = self._call("advertised")
        return bool(self._unwrap(resp, "advertised"))

    def refresh(self, settle: float = 0.6) -> None:
        resp = self._call("refresh", settle=settle, read_timeout=settle * 3 + self.timeout)
        self._unwrap(resp, "refresh")

    def recent(self, since: float = 0.0, limit: int = 200) -> list:
        resp = self._call("recent", since=since, limit=limit)
        return [tuple(e) for e in self._unwrap(resp, "recent") or []]

    def _state_get(self, key):
        """`_Pages` reads the echo cache through this; one daemon round trip per key."""
        return self.state(key)

    # ---- extras, not on osc.LogicPad but useful for a client of the daemon
    def status(self) -> dict:
        resp = self._call("status")
        return self._unwrap(resp, "status")

    def ping(self) -> bool:
        try:
            resp = self._call("ping")
        except PaddUnavailable:
            return False
        return bool(resp.get("ok")) and resp.get("pong") is True

    def close(self) -> None:
        with self._lock:
            self._disconnect()


# --------------------------------------------------------------------------- the seam
def acquire(prefer_daemon: bool = True, socket_path: str = P.PADD_SOCKET, **logicpad_kwargs):
    """(pad, how): a `RemoteLogicPad` talking to a live logicpadd when one answers, else a
    directly-constructed `osc.LogicPad` -- exactly what callers got before this module existed.

    `how` is "daemon" or "direct". With no daemon running this must never behave worse than a bare
    `osc.LogicPad(**logicpad_kwargs)` call, so the direct path takes the same kwargs.
    """
    if prefer_daemon:
        remote = RemoteLogicPad(socket_path=socket_path)
        if remote.ping():
            return remote, "daemon"
        remote.close()
    return osc.LogicPad(**logicpad_kwargs), "direct"


# --------------------------------------------------------------------------- CLI
def _install_signal_handlers(server: PaddServer) -> None:
    def _handle(signum, _frame):
        print(f"[logicpadd] signal {signum}: shutting down (Bonjour advert stays up)", file=sys.stderr)
        server.stop()
        raise SystemExit(0)
    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="logicpadd",
        description="Own the LogicPad OSC surface and serve it to multiple clients over a unix socket.")
    parser.add_argument("--socket", default=P.PADD_SOCKET, help="unix socket path (default: %(default)s)")
    parser.add_argument("--port", type=int, default=osc.DEFAULT_PORT,
                         help="UDP port for the LogicPad OSC surface (default: %(default)s)")
    args = parser.parse_args(argv)

    print(f"[logicpadd] starting: socket={args.socket} port={args.port}", file=sys.stderr)
    try:
        pad = osc.LogicPad(port=args.port)
    except OSError as e:
        print(f"[logicpadd] could not open the LogicPad OSC surface: {e}", file=sys.stderr)
        return 1

    server = PaddServer(pad, socket_path=args.socket)
    try:
        server.start()
    except PaddAlreadyRunning as e:
        print(f"[logicpadd] {e}", file=sys.stderr)
        pad.close()
        return 1

    pad.advertise()
    _install_signal_handlers(server)
    print(f"[logicpadd] listening on {args.socket} -- pad ip={pad.ip} port={pad.port}", file=sys.stderr)
    try:
        while not server._stop.is_set():
            time.sleep(0.5)
    except KeyboardInterrupt:
        server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
