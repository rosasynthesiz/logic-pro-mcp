"""OSC channel: present ourselves to Logic as a TouchOSC "LogicPad" and talk OSC over UDP.

How Logic actually discovers a TouchOSC device (measured on Creator Studio 12.3, see docs/osc.md):

1. Logic browses Bonjour `_osc._udp`. It pattern-matches the SERVICE NAME the way TouchOSC Mk1 announced
   itself: "<name> [iPad] (TouchOSC)" -> model "TouchOSC LogicPad"; "[iPhone]" -> "TouchOSC LogicTouch".
   The model strings inside TouchOSC.bundle are display names only; advertising "TouchOSC LogicPad" is
   found and resolved but never bound.
2. Logic resolves the service to host:port and remembers it (Host Name / Output Port in Setup).
3. The first OSC packet that arrives on Logic's fixed receiver (UDP 7000) FROM that resolved host:port
   raises the alert "OSC app "TouchOSC" on "<name>" wants to connect to Logic Pro." Connect / Don't connect.
   Packets from any other source are dropped silently. Loopback (127.x) is rejected; use the LAN address.
4. After Connect, Logic creates the device (persisted in com.apple.logic.pro.cs), starts
   "OSC ports In=7000, out=<our port>" and pushes its full state (~340 messages), then streams feedback.

Address map (fixed, no Learn step) is in ../osc_map.txt, read out of the plug-in binary. What each page
actually carries (measured, not guessed):
  /1  mixer bank of 8: play/stop/record/cycle, level1..8, volume1..8, select|mute|solo/1/N, bank+/-,
      track+/-, mastervolume, trackname1..8, selectlabel1..8, bar, time
  /2  the same bank plus per-strip pan and 3 sends each (pan1..8, send1..24, auxvolume/auxname)
  /3  the SELECTED track's channel strip: insertname1..12, insertbypass, the focused plug-in's name and
      its 16 parameters (parname/par/value) with page+/page-, sends, pan, mute/solo/recenable
  /4  the selected track's Channel EQ: 8 bands, gain/freq/q (+slope on the filters), master gain
  /5  a second plug-in parameter page (24 slots) -- unused by us, /3 covers it

Zero dependencies: a minimal OSC 1.0 codec, one UDP socket, and `dns-sd -P` for the Bonjour proxy record.
"""
from __future__ import annotations

import socket
import struct
import subprocess
import threading
import time
from collections import deque
from typing import Callable

SERVICE_TYPE = "_osc._udp"
IDENTITY = "LogicPad [iPad] (TouchOSC)"   # the Mk1 announcement format Logic pattern-matches
BONJOUR_HOST = "logicpad.local"           # shows up as "Host Name" in Control Surface Setup
LOGIC_IN_PORT = 7000                      # Logic's OSC receiver ("Starting OSC ports ... In=7000")
DEFAULT_PORT = 9000                       # our feedback port (Logic: "out=9000")

TRANSPORT = {"play": "/1/play", "stop": "/1/stop", "record": "/1/record", "cycle": "/1/cycle",
             "rewind": "/1/rewind", "forward": "/1/forward", "click": "/1/click",
             "bank+": "/1/bank+", "bank-": "/1/bank-", "track+": "/1/track+", "track-": "/1/track-",
             "soloreset": "/1/soloreset"}


# ---------------------------------------------------------------- OSC 1.0 wire format
def _pad(b: bytes) -> bytes:
    """OSC string padding: always at least one NUL terminator, then out to a 4-byte boundary."""
    return b + b"\x00" * (4 - len(b) % 4)


def _pad_blob(b: bytes) -> bytes:
    """OSC blob padding: NO terminator, only alignment. Reusing _pad() here adds a whole spurious
    word when the blob is already aligned, which desyncs every argument after it."""
    return b + b"\x00" * ((4 - len(b) % 4) % 4)


def encode(address: str, *args) -> bytes:
    tags, payload = ",", b""
    for a in args:
        if isinstance(a, bool):
            tags += "T" if a else "F"
        elif isinstance(a, int):
            tags += "i"; payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"; payload += struct.pack(">f", a)
        elif isinstance(a, str):
            tags += "s"; payload += _pad(a.encode())
        elif isinstance(a, bytes):
            tags += "b"; payload += struct.pack(">i", len(a)) + _pad_blob(a)
        else:
            raise TypeError(f"unsupported OSC argument {a!r}")
    return _pad(address.encode()) + _pad(tags.encode()) + payload


def _read_str(buf: bytes, i: int) -> tuple[str, int]:
    end = buf.index(b"\x00", i)
    s = buf[i:end].decode(errors="replace")
    return s, i + ((end - i) // 4 + 1) * 4


def decode(buf: bytes) -> list[tuple[str, list]]:
    """Messages in a packet, bundles flattened."""
    if buf.startswith(b"#bundle"):
        out, i = [], 16
        while i + 4 <= len(buf):
            (n,) = struct.unpack(">i", buf[i:i + 4]); i += 4
            out.extend(decode(buf[i:i + n])); i += n
        return out
    addr, i = _read_str(buf, 0)
    if i >= len(buf) or buf[i:i + 1] != b",":
        return [(addr, [])]
    tags, i = _read_str(buf, i)
    args = []
    for t in tags[1:]:
        if t == "i":
            args.append(struct.unpack(">i", buf[i:i + 4])[0]); i += 4
        elif t == "f":
            args.append(struct.unpack(">f", buf[i:i + 4])[0]); i += 4
        elif t == "s":
            s, i = _read_str(buf, i); args.append(s)
        elif t == "b":
            (n,) = struct.unpack(">i", buf[i:i + 4]); i += 4
            args.append(buf[i:i + n]); i += ((n + 3) // 4) * 4
        elif t == "T": args.append(True)
        elif t == "F": args.append(False)
        elif t == "N": args.append(None)
        elif t in ("I", "[", "]"):
            args.append(None)                      # zero-width tags: no payload to advance past
        else:
            # An unhandled tag has an unknown payload width, so the cursor is now meaningless and
            # every later argument would be silently misparsed. Return what is certain: the address.
            raise ValueError(f"unsupported OSC type tag {t!r} in {addr}")
    return [(addr, args)]


# ---------------------------------------------------------------- host address
def lan_ip() -> str | None:
    """This Mac's LAN IPv4, as Logic must be able to reach it. 127.x is never returned.

    Physical interfaces are asked FIRST, deliberately. Inferring the address from a UDP connect() to a
    guessed far-off address returns whatever the routing table prefers, which on a machine with a VPN up
    is the tunnel address (utun*). Logic would then resolve the Bonjour record to an address it cannot
    reach, and the device would never bind -- with no error anywhere to explain it.
    """
    for dev in ("en0", "en1", "en2", "en3", "en4"):
        r = subprocess.run(["ipconfig", "getifaddr", dev], capture_output=True, text=True)
        ip = r.stdout.strip()
        if ip and not ip.startswith("127."):
            return ip
    try:                                          # fallback: routing hint, may be a tunnel address
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))          # no packet is sent; just picks the outbound interface
        ip = s.getsockname()[0]
        s.close()
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    return None


_LOCAL_IPS_TTL = 3.0
_local_ips_cache: tuple[float, frozenset[str]] = (0.0, frozenset())


def local_ips() -> frozenset[str]:
    """Every IPv4 address this Mac holds right now, loopback excluded. Cached for a few seconds because
    `connected` is polled at gesture rates and `ifconfig` is a process spawn."""
    global _local_ips_cache
    at, ips = _local_ips_cache
    if time.time() - at < _LOCAL_IPS_TTL:
        return ips
    found = set()
    try:
        out = subprocess.run(["ifconfig"], capture_output=True, text=True).stdout
    except OSError:
        out = ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "inet" and not parts[1].startswith("127."):
            found.add(parts[1])
    _local_ips_cache = (time.time(), frozenset(found))
    return _local_ips_cache[1]


# ---------------------------------------------------------------- the device
class LogicPad:
    """A fake TouchOSC LogicPad: listens for Logic's feedback, sends control messages to Logic."""

    def __init__(self, port: int = DEFAULT_PORT, ip: str | None = None, identity: str = IDENTITY):
        self.identity, self.port = identity, port
        self.ip = ip or lan_ip()
        if self.ip is None:
            raise OSError("no LAN IPv4 address; Logic will not bind an OSC device on loopback")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", port))
        self.sock.settimeout(0.2)
        self.log: deque = deque(maxlen=5000)
        self.state: dict[str, list] = {}
        self.last_rx: float = 0.0
        self.rx_count: int = 0
        self.decode_errors: int = 0
        self.rejected: int = 0                    # datagrams from hosts that are not Logic on this Mac
        self._lock = threading.Lock()
        self._adv: subprocess.Popen | None = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    # ---- Bonjour
    def _advert_pids(self) -> list[str]:
        ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True).stdout
        return [line.split()[0] for line in ps.splitlines()      # literal match: identity has regex chars
                if f"dns-sd -P {self.identity} " in line]

    def advertise(self) -> None:
        """Proxy-register the service under a host name that is not this Mac's own (Logic skips its own).

        An advert that is already running is ADOPTED, never restarted: Logic segfaults (measured: SIGSEGV
        on the main thread, 12.3) when the Bonjour service of a bound OSC device disappears. For the same
        reason close() leaves the advert alive; use forget() only while Logic is not running.
        """
        if self._adv is not None and self._adv.poll() is None:
            return
        if self._advert_pids():
            return
        self._adv = subprocess.Popen(
            ["dns-sd", "-P", self.identity, SERVICE_TYPE, "local", str(self.port), BONJOUR_HOST, self.ip],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

    def forget(self) -> int:
        """Withdraw the Bonjour service (kills every advert for this identity). NOT while Logic runs."""
        pids = self._advert_pids()
        for pid in pids:
            subprocess.run(["kill", pid], capture_output=True)
        self._adv = None
        return len(pids)

    @property
    def advertised(self) -> bool:
        return (self._adv is not None and self._adv.poll() is None) or bool(self._advert_pids())

    # ---- receive
    def _trusted_source(self, host: str) -> bool:
        """Only Logic on this Mac may feed the echo cache.

        Everything this channel reports as `confirmed` is read back from that cache, so a datagram from
        any other host on the Wi-Fi would let it forge confirmations. Logic sends from the same address
        we advertised (measured: `192.168.1.31:54101 -> 192.168.1.31:9000`); loopback is allowed for
        the tests and for a daemon on the same box.
        """
        return host == self.ip or host.startswith("127.")

    def _ingest(self, buf: bytes, addr: tuple, now: float | None = None) -> bool:
        """One datagram into the cache. Returns False when it was rejected or unreadable."""
        if not self._trusted_source(addr[0]):
            self.rejected += 1
            return False
        now = time.time() if now is None else now
        try:
            msgs = decode(buf)
        except Exception:
            # A malformed or truncated packet must never take the receive thread down: without
            # this the channel goes permanently deaf while still reporting itself connected.
            self.decode_errors += 1
            return False
        self.last_rx = now
        with self._lock:
            for a, args in msgs:
                self.rx_count += 1
                self.log.append((now, a, args))
                self.state[a] = args
        return True

    def _pump(self) -> None:
        while not self._stop.is_set():
            try:
                buf, addr = self.sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break
            self._ingest(buf, addr)

    def bound(self) -> bool:
        """Passive check: once Logic binds the device it keeps a UDP socket connected to our ip:port."""
        r = subprocess.run(["lsof", "-nP", f"-iUDP@{self.ip}:{self.port}"], capture_output=True, text=True)
        return any("->" in line and line.rstrip().endswith(f":{self.port}") for line in r.stdout.splitlines())

    LIVE_WINDOW = 90.0        # seconds of silence after which we stop trusting "it talked to us"

    @property
    def ip_stale(self) -> bool:
        """The address we advertised and send from no longer belongs to this Mac.

        Measured 2026-10-06: the Wi-Fi lease changed mid-session (10.179.131.116 -> 10.213.26.157).
        Logic's device socket stayed connected to the old address, so `bound()` kept saying yes, while
        every send() went to a host that no longer existed and Logic -- which drops packets from any
        source but the one it resolved -- never heard another word. Nothing short of quitting Logic,
        withdrawing the advert and re-binding on the new address recovers from it.
        """
        return self.ip not in local_ips()

    @property
    def connected(self) -> bool:
        """Is Logic bound RIGHT NOW -- recent traffic, else a live socket check.

        This deliberately does NOT latch on `rx_count > 0`. Logic only sends when something changes, so
        silence is normal; but a cumulative counter can never fall back to false, and once it latched,
        `_osc_ready` skipped re-binding forever and every later call reported confident results from a
        frozen cache after Logic had quit.
        """
        if self.ip_stale:
            return False
        if self.last_rx and (time.time() - self.last_rx) < self.LIVE_WINDOW:
            return True
        return self.bound()

    # ---- send
    def send(self, address: str, *args) -> bool:
        try:
            self.sock.sendto(encode(address, *args), (self.ip, LOGIC_IN_PORT))
            return True
        except OSError:
            return False

    def press(self, address: str) -> bool:
        """Button semantics of the LogicPad layout: 1.0 = press (Logic acts on the press, ignores 0.0)."""
        return self.send(address, 1.0)

    def refresh(self, settle: float = 0.6) -> None:
        """Make Logic resend the bank state (it only pushes it on device creation): step the bank away and back."""
        self.press("/1/bank+"); time.sleep(settle)
        self.press("/1/bank-"); time.sleep(settle)

    def wait_for(self, address: str, timeout: float = 1.5, since: float | None = None):
        """Block until Logic echoes `address` (feedback), returning its args or None."""
        t0 = since if since is not None else time.time()
        end = time.time() + timeout
        while time.time() < end:
            with self._lock:                       # the pump appends concurrently; iterating a deque
                snap = list(self.log)              # while it mutates raises RuntimeError
            for ts, a, args in reversed(snap):
                if ts < t0:
                    break
                if a == address:
                    return args
            time.sleep(0.03)
        return None

    def recent(self, since: float = 0.0, limit: int = 200) -> list:
        with self._lock:
            snap = list(self.log)
        return [e for e in snap if e[0] >= since][-limit:]

    # ---- connection handshake
    def connect(self, press_alert: Callable[[], bool] | None = None, timeout: float = 10.0) -> dict:
        """Advertise, poke Logic once, let the caller click Connect on Logic's alert, wait for feedback.

        `press_alert` is polled while waiting; it should press the "Connect" button if the alert is up and
        return True when it did. Returns {connected, alert_pressed, feedback, ip, port, seconds}.
        """
        t0 = time.time()
        self.advertise()
        time.sleep(2.0)                              # let Logic find + resolve the service
        pressed = False
        end = t0 + timeout
        poke_at = 0.0
        while time.time() < end and not self.connected:
            if time.time() >= poke_at:               # only reached while Logic has NOT bound us: a real
                self.press("/1/bank-")               # press is needed to raise the connect alert (0.0 is
                poke_at = time.time() + 1.5          # ignored); bank- is view-only and a no-op at bank 0
            if press_alert is not None and not pressed:
                try:
                    pressed = bool(press_alert())
                except Exception:
                    pressed = False
            time.sleep(0.25)
        if self.connected:
            time.sleep(1.0)                          # drain the initial state burst
        return {"connected": self.connected, "alert_pressed": pressed, "feedback": self.rx_count,
                "ip": self.ip, "port": self.port, "seconds": round(time.time() - t0, 1)}

    # ---- readback helpers
    def snapshot(self) -> dict:
        """Compact view of what Logic last told us (page 1 = mixer bank of 8)."""
        s = self.state
        first = lambda k, d=None: (s.get(k) or [d])[0]  # noqa: E731
        bank = []
        for n in range(1, 9):
            bank.append({
                "n": n,
                "name": first(f"/1/trackname{n}"),
                "label": first(f"/1/selectlabel{n}"),
                "track_no": first(f"/1/track#{n}"),
                "selected": first(f"/1/select/1/{n}"),
                "mute": first(f"/1/mute/1/{n}"),
                "solo": first(f"/1/solo/1/{n}"),
                "volume": first(f"/1/volume{n}"),
                "level": first(f"/1/level{n}"),
            })
        return {
            "connected": self.connected, "feedback": self.rx_count,
            "age": round(time.time() - self.last_rx, 1) if self.last_rx else None,
            "transport": {k: first(TRANSPORT[k]) for k in ("play", "stop", "record", "cycle", "click")},
            "bar": first("/1/bar"), "time": first("/1/time"),
            "master": {"name": first("/1/mastername"), "volume": first("/1/mastervolume"),
                       "level": first("/1/masterlevel")},
            "bank": bank,
        }

    def close(self) -> None:
        """Stop listening. The Bonjour advert deliberately stays up (see advertise())."""
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass


# ---------------------------------------------------------------- pages 2-5: strip, EQ, plug-in parameters
# Logic pushes a page's whole state when the surface reports a page switch: a bare "/N" message.
# Logic abbreviates plug-in parameter names on the control surface ("Threshold" -> "Thrs"), so a recipe
# written in full words finds nothing. Map the common full names onto Logic's short forms; matching still
# falls back to substring, so anything not listed here keeps working as before.
PARAM_ALIASES = {
    "threshold": ("thrs", "thresh"),
    "frequency": ("frq", "freq", "detctrf"),          # DeEsser calls it Detector Frequency
    "gain": ("gai",),
    "makeup": ("make up",),
    "make-up": ("make up",),
    "make up gain": ("make up",),
    "reduction": ("redctn", "maxredct", "range"),
    "range": ("maxredct", "redctn"),                  # DeEsser's Range is Max Reduction
    "max reduction": ("maxredct",),
    "sensitivity": ("sens",),
    "smoothing": ("smooth",),
    "detector": ("detect", "sichdetc"),
    "output": ("output", "outlevel", "out gain", "outgain"),
    "output level": ("outlevel", "output"),
    "gain reduction": ("gainredc",),
    "lookahead": ("lookahad",),
    "release": ("release", "rel"),
    "attack": ("attack", "att"),
    "ratio": ("ratio",),
    "knee": ("knee",),
    "mix": ("mix",),
}


# Multiband plug-ins name every parameter per band ("Ba1CompT".."Ba4CompT"), so a recipe written for a
# single-band compressor finds nothing. These patterns expand a generic name onto every band.
BAND_PATTERNS = {
    "threshold": ("compt",),
    "ratio": ("compr",),
    "gain": ("makup",),
    "make up": ("makup",),
    "attack": ("at",),
    "release": ("rel",),
}


def resolve_band_params(name: str, params: list[dict]) -> list[dict]:
    """Every per-band parameter a generic name refers to, e.g. Threshold -> Ba1..Ba4CompT."""
    want = name.strip().lower().replace(" ", "")
    out = []
    for suffix in BAND_PATTERNS.get(name.strip().lower(), ()):
        hits = [p for p in params
                if p["name"].strip().lower().startswith(("ba", "band"))
                and p["name"].strip().lower().rstrip("0123456789").endswith(suffix)]
        if not hits:
            hits = [p for p in params
                    if p["name"].strip().lower().startswith(("ba", "band")) and suffix in p["name"].strip().lower()]
        if len(hits) > 1:
            return sorted(hits, key=lambda p: p["n"])
    return out


def resolve_param(name: str, params: list[dict]) -> dict | None:
    """Find a parameter by full name, Logic's abbreviation, or a substring -- in that order."""
    want = name.strip().lower()
    for p in params:                                   # exact
        if p["name"].strip().lower() == want:
            return p
    for alias in PARAM_ALIASES.get(want, ()):          # known Logic short form
        for p in params:
            if p["name"].strip().lower() == alias:
                return p
        for p in params:
            if alias in p["name"].strip().lower():
                return p
    for p in params:                                   # substring, either direction
        n = p["name"].strip().lower()
        if want in n or n in want:
            return p
    return None


EQ_BANDS = {1: "hpf", 2: "loslv", 3: "lo", 4: "lomid", 5: "himid", 6: "hi", 7: "hislv", 8: "lpf"}
EQ_BAND_LABEL = {1: "high-pass", 2: "low shelf", 3: "peak 1", 4: "peak 2", 5: "peak 3", 6: "peak 4",
                 7: "high shelf", 8: "low-pass"}
_EQ_Q = {"lo": "lowq"}                     # the one irregular control name
_EQ_QVAL = {"lo": "loqval"}


def _num(text) -> float | None:
    """First number in a display string, normalised so values of the same kind compare directly.

    Logic and third-party plug-ins switch units as the value grows -- FabFilter Pro-C 2 shows "2.500sec"
    where the recipe asks for milliseconds -- so a raw number comparison silently fails. Seconds become
    milliseconds and kHz becomes Hz; everything else is returned as written.
    """
    import re
    if text is None:
        return None
    t = str(text)
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    if not m:
        return None
    v = float(m.group())
    tail = t[m.end():].strip().lower()
    if tail.startswith("k"):                       # 1.20k / 1.2 kHz -> Hz
        v *= 1000.0
    elif re.match(r"^(s|sec|secs|seconds)\b", tail):   # 2.500sec -> ms (never matches "ms")
        v *= 1000.0
    return v


class _Pages:
    """Mixed into LogicPad: page views and closed-loop parameter setting."""

    def page(self, n: int, settle: float = 0.9) -> float:
        """Switch the surface to page n. Logic pushes that page's state, but ONLY values that changed since
        it last sent them (it de-duplicates per address) -- a fresh process must call full_refresh() once.
        """
        t0 = time.time()
        self.send(f"/{n}")
        self._page = n
        time.sleep(settle)
        return t0

    def ensure_page(self, n: int, settle: float = 0.5) -> bool:
        """Put the surface on page n before addressing it.

        Logic routes by page: while the surface is on page 3, a /1/play is DROPPED without a word. The
        page survives our process (Logic remembers it), so a fresh device must assert it rather than
        assume page 1. Returns True when a switch was actually sent.
        """
        if getattr(self, "_page", None) == n:
            return False
        self.page(n, settle=settle)
        return True

    def full_refresh(self, settle: float = 0.7) -> dict:
        """Make Logic resend every selected-track value: hop the selection one track away and back.

        Logic's per-address de-duplication means a restarted process sees nothing until values change;
        changing the selected track changes them all. View-only side effect (the track list may scroll).
        """
        self.page(3, settle=0.5)
        no = _num(self._first("/3/track#"))
        first, second = ("/3/track-", "/3/track+") if (no or 0) > 1 else ("/3/track+", "/3/track-")
        self.press(first); time.sleep(settle)
        self.press(second); time.sleep(settle)
        after = _num(self._first("/3/track#"))
        self.page(1, settle=0.4)                     # never leave the surface off page 1: /1/* is dropped
        return {"track_before": no, "track_after": after, "restored": no is None or after == no}

    def _state_get(self, key):
        """The echo cache's entry for `key`. LogicPad holds the dict itself; RemoteLogicPad overrides
        this to ask the daemon, which is what lets the page views below run unchanged over the socket."""
        return self.state.get(key)

    def _first(self, key, default=None):
        v = self._state_get(key)
        return v[0] if v else default

    def strip_view(self) -> dict:
        """Page 3: the selected track's inserts, focused plug-in parameters, sends, pan, mute, solo, arm."""
        self.page(3)
        f = self._first
        params = []
        for n in range(1, 17):
            name = f(f"/3/parname{n}")
            if name not in (None, "", "-"):
                params.append({"n": n, "name": name, "norm": f(f"/3/par{n}"), "value": f(f"/3/value{n}")})
        inserts = []
        for s in range(1, 13):
            name = f(f"/3/insertname{s}")
            if name:
                on = f(f"/3/insertbypass/{13 - s}/1")            # TouchOSC grid: slot 1 is column 12
                inserts.append({"slot": s, "name": name, "on": on, "bypassed": None if on is None else on == 0.0})
        sends = [{"n": n, "name": f(f"/3/sendname{n}"), "level": f(f"/3/sendlevel{n}"), "value": f(f"/3/sendval{n}")}
                 for n in range(1, 9) if f(f"/3/sendname{n}")]
        return {"track_no": f("/3/track#"), "track": f("/3/trackname"), "plugin": f("/3/pluginname"),
                "page": f("/3/page#"), "inserts": inserts, "params": params, "sends": sends,
                "pan": f("/3/pan"), "pan_value": f("/3/trkpanval"), "volume": f("/3/volume"),
                "volume_value": f("/3/trkvolval"), "mute": f("/3/mute"), "solo": f("/3/solo"),
                "recenable": f("/3/recenable")}

    def eq_refresh(self, settle: float = 0.7) -> None:
        """Force Logic to resend page 4 by hopping the selected track while page 4 is showing.

        Needed because Logic only sends a value that CHANGED. On a freshly bound device every EQ display
        string is still empty, which eq_view would otherwise read as "this track has no Channel EQ" --
        a confident false negative. full_refresh() does not cover this: it hops the track while page 3
        is showing, and Logic only pushes the page the surface is actually on.
        """
        self.page(4, settle=0.4)
        no = _num(self._first("/4/track#"))
        first, second = ("/4/track-", "/4/track+") if (no or 0) > 1 else ("/4/track+", "/4/track-")
        self.press(first); time.sleep(settle)
        self.press(second); time.sleep(settle)

    def eq_view(self, refresh: bool = True) -> dict:
        """Page 4: Channel EQ of the selected track (empty display values = no Channel EQ on the track)."""
        self.page(4)
        f = self._first
        bands = []
        for n, b in EQ_BANDS.items():
            bands.append({
                "band": n, "label": EQ_BAND_LABEL[n], "on": f(f"/4/eqbandbyp/1/{n}"),
                "gain": f(f"/4/gain/{n}"), "gain_value": f(f"/4/{b}gainval"),
                "freq": f(f"/4/{b}frq"), "freq_value": f(f"/4/{b}frqval"),
                "q": f(f"/4/{_EQ_Q.get(b, b + 'q')}"), "q_value": f(f"/4/{_EQ_QVAL.get(b, b + 'qval')}"),
                **({"slope": f(f"/4/{b}slp"), "slope_value": f(f"/4/{b}slpval")} if b in ("hpf", "lpf") else {}),
            })
        present = any(bd["freq_value"] for bd in bands)
        if not present and refresh:
            # Never report "no Channel EQ" off an un-refreshed page: prove it by forcing a resend first.
            self.eq_refresh()
            return self.eq_view(refresh=False)
        return {"track_no": f("/4/track#"), "track": f("/4/trackname"), "present": present,
                "master_gain": f("/4/eqmstgain"), "master_gain_value": f("/4/eqmstgainval"),
                "q_coupling": f("/4/qcoupling"), "bands": bands}

    def eq_address(self, band: int, param: str) -> tuple[str, str]:
        """(control address, display-value address) for band 1..8 and param gain|freq|q|slope|on."""
        b = EQ_BANDS[band]
        if param == "gain":
            if b in ("hpf", "lpf"):
                raise ValueError(f"band {band} ({EQ_BAND_LABEL[band]}) has no gain, only freq/q/slope")
            return f"/4/gain/{band - 1}", f"/4/{b}gainval"      # measured: /4/gain/3 drives band 4, 0.5 = 0 dB
        if param == "freq":
            return f"/4/{b}frq", f"/4/{b}frqval"
        if param == "q":
            return f"/4/{_EQ_Q.get(b, b + 'q')}", f"/4/{_EQ_QVAL.get(b, b + 'qval')}"
        if param == "slope" and b in ("hpf", "lpf"):
            return f"/4/{b}slp", f"/4/{b}slpval"
        if param == "on":
            return f"/4/eqbandbyp/1/{band}", f"/4/eqbandbyp/1/{band}"
        raise ValueError(f"no {param} on band {band} ({EQ_BAND_LABEL[band]})")

    def set_norm(self, control: str, display: str, norm: float, timeout: float = 1.2) -> dict:
        """Send a normalised 0..1 value and return what Logic shows for it."""
        t0 = time.time()
        self.send(control, float(max(0.0, min(1.0, norm))))
        shown = self.wait_for(display, timeout=timeout, since=t0)
        return {"norm": norm, "value": shown[0] if shown else None, "echoed": shown is not None}

    def set_target(self, control: str, display: str, target: float, steps: int = 14,
                   tolerance: float = 0.0, settle: float = 0.12) -> dict:
        """Closed loop: bisect the normalised value until Logic's display value reaches `target`.

        Handles parameters that run BACKWARDS (Logic's DeEsser MaxRedct is 25 dB at 0.0 and 0 dB at 1.0);
        assuming they increase walks the bisection away from the target and silently lands anywhere.
        """
        def probe(norm, tries: int = 2):
            """Send a value and read back what Logic ECHOES for it.

            Falling back to self.state on no echo returns the previous value, which then poisons the
            direction test -- a stale endpoint reading made Limiter OutLevel look like it fell when it
            rises, and the bisection parked on the wrong end.
            """
            for attempt in range(tries):
                t0 = time.time()
                self.send(control, float(max(0.0, min(1.0, norm))))
                shown = self.wait_for(display, timeout=0.8, since=t0)
                time.sleep(settle)
                if shown is not None:
                    return shown[0], _num(shown[0])
            cached = self._state_get(display)           # unchanged value: Logic never re-sends it
            return (cached[0] if cached else None), (_num(cached[0]) if cached else None)

        lo_txt, lo_val = probe(0.0)
        hi_txt, hi_val = probe(1.0)
        if lo_val is None or hi_val is None:
            return {"ok": False, "reason": "no display value from Logic", "control": control}
        rising = hi_val >= lo_val
        span = abs(hi_val - lo_val)
        if target < min(lo_val, hi_val) - 1e-9 or target > max(lo_val, hi_val) + 1e-9:
            return {"ok": False, "reason": f"target {target} outside this parameter's range "
                                           f"{min(lo_val, hi_val)}..{max(lo_val, hi_val)}",
                    "control": control, "value": hi_txt, "number": hi_val}
        lo, hi, best = 0.0, 1.0, None
        for _ in range(steps):
            mid = (lo + hi) / 2
            txt, v = probe(mid)
            if v is None:
                return {"ok": False, "reason": "no display value from Logic", "control": control}
            best = (mid, txt, v)
            if abs(v - target) <= tolerance:
                break
            if (v < target) == rising:                 # move the end that walks us toward the target
                lo = mid
            else:
                hi = mid
        # Scale ONLY with the target, never with the parameter's full span: a 0-1000 ms Attack gave a
        # 5 ms tolerance, so 0 ms passed as "confirmed" for a 0.5 ms target.
        tol = max(tolerance, abs(target) * 0.03 + 0.02)
        return {"ok": best is not None and abs(best[2] - target) <= tol,
                "norm": best[0], "value": best[1], "number": best[2], "target": target,
                "direction": "rising" if rising else "falling"}

    # ---- page 3 plug-in parameters and insert slots
    def param_address(self, n: int) -> tuple[str, str]:
        return f"/3/par{n}", f"/3/value{n}"

    @staticmethod
    def insert_address(slot: int, what: str = "bypass") -> str:
        """Insert slot 1..12 -> grid column 13-slot (measured: slot 1 echoes as /3/insertbypass/12/1)."""
        return f"/3/{'insertbypass' if what == 'bypass' else 'mInsertEdit'}/{13 - slot}/1"


for _name, _fn in list(_Pages.__dict__.items()):
    if not _name.startswith("__"):
        setattr(LogicPad, _name, _fn)
