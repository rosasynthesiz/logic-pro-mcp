"""CoreMIDI channel via python-rtmidi.

Two paths into Logic:
  1. 'Logic Pro Virtual In' — Logic's own always-on input. Notes/CC land on the record-armed track.
  2. MCU emulation — we publish virtual ports; user adds a Mackie Control in Logic > Settings > Control Surfaces
     pointing at them. Then transport/mixer commands work and Logic echoes LED + LCD state back (readback).

Only ONE process on the Mac may publish the MCU ports. Every MCP client (Claude, Hermes, a second Hermes
over SSH, a doctor run) starts its own server, and each extra copy of the ports reaches Logic without the
fixed unique IDs (the first owner holds them), so Logic treats it as new hardware and silently installs
another Mackie Control chained at the next bank offset. Three of them on one port garble the LCD and move
faders on the wrong tracks. Ownership is an exclusive flock held for the life of the process; a process
that can't get it, or that sees the ports already published by an older server, leaves MCU alone and the
callers fall back to OSC / Accessibility.
"""
from __future__ import annotations

import fcntl
import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import rtmidi

LOGIC_IN_NAME = "Logic Pro Virtual In"
MCU_PORT = "LogicMCP MCU"

# Mackie Control transport buttons (note numbers, channel 1). Press = vel 127, release = vel 0.
MCU_BUTTONS = {
    "rewind": 0x5B, "fast_forward": 0x5C, "stop": 0x5D, "play": 0x5E, "record": 0x5F,
    "cycle": 0x56, "marker": 0x54, "nudge": 0x55, "drop": 0x57, "replace": 0x58, "click": 0x59, "solo_led": 0x5A,
    "up": 0x60, "down": 0x61, "left": 0x62, "right": 0x63, "zoom": 0x64, "scrub": 0x65,
    "bank_left": 0x2E, "bank_right": 0x2F, "channel_left": 0x30, "channel_right": 0x31, "flip": 0x32, "global": 0x33,
    "f1": 0x36, "f2": 0x37, "f3": 0x38, "f4": 0x39, "f5": 0x3A, "f6": 0x3B, "f7": 0x3C, "f8": 0x3D,
    "save": 0x50, "undo": 0x51, "cancel": 0x52, "enter": 0x53,
}
# Per-strip buttons: base + strip index (0-7)
MCU_STRIP = {"rec": 0x00, "solo": 0x08, "mute": 0x10, "select": 0x18}
MCU_LED_NAMES = {v: k for k, v in MCU_BUTTONS.items()}

MMC = {"stop": 0x01, "play": 0x02, "deferred_play": 0x03, "fast_forward": 0x04, "rewind": 0x05,
       "record_strobe": 0x06, "record_exit": 0x07, "pause": 0x09}

# Mackie Control handshake. Logic broadcasts device queries to every new MIDI port; answering makes
# Logic auto-install a Mackie Control surface bound to our ports. No manual Control Surfaces setup.
MCU_HDR = [0x00, 0x00, 0x66]
MCU_MODEL = 0x14  # Mackie Control (0x10 Logic Control, 0x15 MCU XT)
MCU_SERIAL = [0x4C, 0x4D, 0x43, 0x50, 0x30, 0x30, 0x31]  # "LMCP001"
MCU_CHALLENGE = [0x01, 0x02, 0x03, 0x04]
MCU_VERSION = [0x56, 0x31, 0x2E, 0x30, 0x30]  # "V1.00"
MCU_UID_SRC, MCU_UID_DST = 0x4C4D4331, 0x4C4D4332  # fixed CoreMIDI unique IDs ("LMC1"/"LMC2")
MCU_LOCK_PATH = Path(os.environ.get("LOGIC_MCP_MCU_LOCK", "~/Library/Caches/logic-pro-mcp/mcu.lock")).expanduser()
MCU_FADER_0DB = 12443  # Logic echoes this 14-bit value at +0.0 dB


@dataclass
class McuState:
    leds: dict[str, bool] = field(default_factory=dict)
    handshake: bool = False
    handshake_at: float = 0.0

    def ready(self, settle: float = 1.5) -> bool:
        """Logic ignores MCU buttons for ~1-2s after the handshake."""
        return self.handshake and (time.time() - self.handshake_at) >= settle
    lcd: str = ""
    timecode: str = ""
    faders: dict[int, int] = field(default_factory=dict)
    strips: dict[int, dict] = field(default_factory=dict)
    last_rx: float = 0.0
    rx_count: int = 0
    unavailable: str = ""  # why this process does not own the MCU ports ("" = it does, or hasn't tried)

    @property
    def playing(self) -> bool: return self.leds.get("play", False)
    @property
    def recording(self) -> bool: return self.leds.get("record", False)

    def to_dict(self) -> dict:
        return {"handshake": self.handshake, "playing": self.playing, "recording": self.recording, "cycle": self.leds.get("cycle"),
                "click": self.leds.get("click"), "lcd": self.lcd.strip(), "timecode": self.timecode,
                "faders": self.faders, "strips": self.strips, "rx_count": self.rx_count,
                "age_s": round(time.time() - self.last_rx, 2) if self.last_rx else None,
                **({"unavailable": self.unavailable} if self.unavailable else {})}


class MidiBus:
    def __init__(self) -> None:
        self._out: rtmidi.MidiOut | None = None
        self._mcu_out: rtmidi.MidiOut | None = None
        self._mcu_in: rtmidi.MidiIn | None = None
        self._mcu_lock_fd: int | None = None
        self._open_lock = threading.Lock()  # not self._lock: _on_mcu holds that on the CoreMIDI thread
        self.mcu = McuState()
        self._lcd = [" "] * 112
        self._lock = threading.Lock()
        self.rx_log: list[list[str]] = []
        self.bank: int | None = None

    # ---- ports -------------------------------------------------------------
    @staticmethod
    def list_ports() -> dict:
        return {"outputs": rtmidi.MidiOut().get_ports(), "inputs": rtmidi.MidiIn().get_ports()}

    def open_logic_in(self) -> bool:
        if self._out:
            return True
        mo = rtmidi.MidiOut()
        for i, name in enumerate(mo.get_ports()):
            if LOGIC_IN_NAME in name:
                mo.open_port(i)
                self._out = mo
                return True
        return False

    def _acquire_mcu_lock(self) -> bool:
        """Exclusive, non-blocking, held until close() or process exit (the kernel drops it on death).
        Caller holds self._open_lock."""
        if self._mcu_lock_fd is not None:
            return True
        try:
            MCU_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(MCU_LOCK_PATH, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:  # fail closed: without the lock we can't know we're alone
            self.mcu.unavailable = f"MCU ownership lock unavailable ({e}); not publishing MCU ports."
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            holder = ""
            try:
                holder = os.pread(fd, 32, 0).decode(errors="replace").strip()
            except OSError:
                pass
            os.close(fd)
            self.mcu.unavailable = (f"MCU ports are owned by another logic-pro-mcp process (pid {holder or '?'}); "
                                    "this one uses OSC/Accessibility instead. Not a Logic setup problem.")
            return False
        except OSError as e:  # e.g. ENOTSUP on a filesystem without flock: not contention, say what it is
            os.close(fd)
            self.mcu.unavailable = f"MCU ownership lock failed ({e}) on {MCU_LOCK_PATH}; not publishing MCU ports."
            return False
        self._mcu_lock_fd = fd
        try:  # informational only; a failed write must not leak the locked fd
            os.ftruncate(fd, 0); os.pwrite(fd, str(os.getpid()).encode(), 0)
        except OSError:
            pass
        return True

    def _release_mcu_lock(self) -> None:
        if self._mcu_lock_fd is not None:
            try:
                fcntl.flock(self._mcu_lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._mcu_lock_fd)
                self._mcu_lock_fd = None

    @staticmethod
    def _mcu_ports_published() -> bool:
        """True if the MCU ports already exist. Only ever asked before THIS process publishes its own, so a
        hit means another process has them (e.g. a server from before this lock)."""
        try:
            return any(MCU_PORT in p for p in rtmidi.MidiOut().get_ports() + rtmidi.MidiIn().get_ports())
        except Exception:
            return False

    def open_mcu(self) -> bool:
        # Tool calls run on worker threads; two parallel ones (health + transport state on an agent's first
        # turn) must not both publish, nor have one release the lock under the other's live ports.
        with self._open_lock:
            if self._mcu_out and self._mcu_in:
                return True
            if not self._acquire_mcu_lock():
                return False
            if self._mcu_ports_published():
                self._release_mcu_lock()
                self.mcu.unavailable = ("MCU ports are already published by another process; "
                                        "this one uses OSC/Accessibility instead. Not a Logic setup problem.")
                return False
            mo = mi = None
            try:
                mo = rtmidi.MidiOut(); mo.open_virtual_port(MCU_PORT)
                mi = rtmidi.MidiIn(); mi.open_virtual_port(MCU_PORT)
                mi.ignore_types(sysex=False, timing=True, active_sense=True)
                self._mcu_out, self._mcu_in = mo, mi  # before the callback: handshake replies go through _mcu_out
                mi.set_callback(self._on_mcu)
                self._pin_unique_ids()
            except Exception as e:  # never keep the lock (blocking every other server) over a half-open pair
                with self._lock:
                    self._mcu_out = self._mcu_in = None
                for p in (mi, mo):
                    try:
                        p and p.close_port()
                    except Exception:
                        pass
                self._release_mcu_lock()
                self.mcu.unavailable = f"could not publish MCU ports: {e}"
                return False
            self.mcu.unavailable = ""
            return True

    @staticmethod
    def _pin_unique_ids() -> None:
        """Give our virtual endpoints fixed CoreMIDI unique IDs so Logic's saved control-surface binding
        survives server restarts (otherwise every launch is a 'new' device)."""
        try:
            import CoreMIDI as CM
        except ImportError:
            return
        time.sleep(0.05)
        for getn, get, uid in ((CM.MIDIGetNumberOfSources, CM.MIDIGetSource, MCU_UID_SRC),
                               (CM.MIDIGetNumberOfDestinations, CM.MIDIGetDestination, MCU_UID_DST)):
            for i in range(getn()):
                ep = get(i)
                st, name = CM.MIDIObjectGetStringProperty(ep, CM.kMIDIPropertyName, None)
                if st == 0 and name == MCU_PORT:
                    CM.MIDIObjectSetIntegerProperty(ep, CM.kMIDIPropertyUniqueID, uid)

    # ---- MCU bank / strip mapping --------------------------------------------
    # Logic on this setup does NOT respond to the MCU bank buttons: bank_right/channel_right leave the
    # LCD unchanged. Dead-reckoning a bank therefore wrote faders onto whatever track happened to sit on
    # that strip — measured, and it silently changed tracks nobody asked for. So the bank is now READ from
    # the LCD and a strip is handed out only when Logic itself confirms the track is on it.
    def lcd_names(self) -> list[str]:
        """The eight strip names Logic is currently displaying (7 characters per cell)."""
        row = (self.mcu.lcd.split("\n")[0] if self.mcu.lcd else "")
        return [row[i * 7:(i + 1) * 7].strip() for i in range(8)]

    @staticmethod
    def _abbrev_matches(lcd_name: str, track_name: str) -> bool:
        """Logic squeezes names into 6-7 chars by dropping characters, so match as a subsequence."""
        a = "".join(lcd_name.split()).lower()
        b = "".join(track_name.split()).lower()
        if not a:
            return not b
        it = iter(b)
        return all(ch in it for ch in a)

    def bank_offset(self, track_names: list[str]) -> int | None:
        """Which track the leftmost strip is showing, or None when the LCD cannot be reconciled.

        Reconciled means exactly one offset explains all EIGHT cells: a named cell must abbreviate the
        track at that position, and a blank cell must sit past the end of the list. The first version
        returned the first offset whose window matched and only checked as many cells as the window
        had, so a 3-track tail "matched" an LCD showing 8 names, and a project with repeated names
        answered with whichever block came first. Either way mute/solo/arm then hit another track and
        reported `confirmed` for the one asked (eval 2026-09-11). Ambiguity is None: callers fall back
        to Accessibility, which addresses tracks by row, not by bank.
        """
        names = self.lcd_names()
        if not any(names):
            return None
        hits: list[int] = []
        for off in range(0, max(1, len(track_names))):
            window = track_names[off:off + 8]
            if not window:
                break
            ok = True
            for i in range(8):
                if i < len(window):
                    if not self._abbrev_matches(names[i], window[i]):
                        ok = False
                        break
                elif names[i]:                      # a name where the list has no track
                    ok = False
                    break
            if ok:
                hits.append(off)
        return hits[0] if len(hits) == 1 else None

    def strip_for(self, track_index1: int, track_names: list[str] | None = None) -> int | None:
        """MCU strip for a 1-based track, or None when it is not on the surface right now."""
        if track_names is None:                     # no way to verify -> only trust the first bank
            return track_index1 - 1 if 1 <= track_index1 <= 8 else None
        off = self.bank_offset(track_names)
        if off is None:
            return None
        strip = (track_index1 - 1) - off
        return strip if 0 <= strip < 8 else None

    def mcu_select_track(self, track_index1: int, track_names: list[str] | None = None) -> bool:
        strip = self.strip_for(track_index1, track_names)
        return False if strip is None else self.mcu_strip("select", strip)

    # ---- direct MIDI into Logic ---------------------------------------------
    def _send(self, msg: list[int]) -> bool:
        if not self.open_logic_in():
            return False
        self._out.send_message(msg)
        return True

    def note(self, note: int, vel: int = 100, ch: int = 1, dur: float = 0.25) -> bool:
        st = 0x90 | (ch - 1)
        ok = self._send([st, note & 0x7F, vel & 0x7F])
        if ok and dur > 0:
            threading.Timer(dur, lambda: self._send([0x80 | (ch - 1), note & 0x7F, 0])).start()
        return ok

    def chord(self, notes: list[int], vel: int = 100, ch: int = 1, dur: float = 0.5) -> bool:
        return all(self.note(n, vel, ch, dur) for n in notes)

    def sequence(self, steps: list[dict], bpm: float = 120.0, ch: int = 1) -> bool:
        """steps: [{note|notes, vel?, beats?}] played in real time. Use for step-recording into an armed track."""
        for s in steps:
            beats = float(s.get("beats", 1)); dur = 60.0 / bpm * beats
            notes = s.get("notes") or [s.get("note")]
            if notes and notes[0] is not None:
                self.chord([int(n) for n in notes], int(s.get("vel", 100)), ch, dur * 0.9)
            time.sleep(dur)
        return True

    def cc(self, num: int, val: int, ch: int = 1) -> bool: return self._send([0xB0 | (ch - 1), num & 0x7F, val & 0x7F])
    def program(self, num: int, ch: int = 1) -> bool: return self._send([0xC0 | (ch - 1), num & 0x7F])
    def aftertouch(self, val: int, ch: int = 1) -> bool: return self._send([0xD0 | (ch - 1), val & 0x7F])
    def pitch_bend(self, val: int, ch: int = 1) -> bool:  # 0..16383, center 8192
        v = max(0, min(16383, val)); return self._send([0xE0 | (ch - 1), v & 0x7F, (v >> 7) & 0x7F])
    def sysex(self, data: list[int]) -> bool:
        return self._send([0xF0, *[b & 0x7F for b in data], 0xF7])

    def mmc(self, cmd: str, device: int = 0x7F) -> bool:
        return self.sysex([0x7F, device, 0x06, MMC[cmd]])

    def mmc_locate(self, h: int, m: int, s: int, f: int, fps_code: int = 0) -> bool:
        return self.sysex([0x7F, 0x7F, 0x06, 0x44, 0x06, 0x01, (fps_code << 5) | h, m, s, f, 0])

    # ---- MCU emulation ------------------------------------------------------
    def wait_ready(self, timeout: float = 4.0) -> bool:
        if not self.open_mcu():
            return False
        end = time.time() + timeout
        while time.time() < end:
            if self.mcu.ready():
                return True
            time.sleep(0.1)
        return self.mcu.ready()

    def mcu_button(self, name: str, hold: float = 0.05) -> bool:
        if name not in MCU_BUTTONS or not self.wait_ready():
            return False
        n = MCU_BUTTONS[name]
        self._mcu_out.send_message([0x90, n, 0x7F]); time.sleep(hold); self._mcu_out.send_message([0x90, n, 0x00])
        return True

    def mcu_strip(self, kind: str, strip: int, hold: float = 0.05) -> bool:
        if kind not in MCU_STRIP or not 0 <= strip < 8 or not self.open_mcu():
            return False
        n = MCU_STRIP[kind] + strip
        self._mcu_out.send_message([0x90, n, 0x7F]); time.sleep(hold); self._mcu_out.send_message([0x90, n, 0x00])
        return True

    def mcu_fader(self, strip: int, value14: int) -> bool:
        """strip 0-7 tracks, 8 master. value 0..16383"""
        if not self.open_mcu():
            return False
        v = max(0, min(16383, value14))
        self._mcu_out.send_message([0xE0 | strip, v & 0x7F, (v >> 7) & 0x7F])
        return True

    def mcu_vpot(self, strip: int, delta: int) -> bool:
        if not self.open_mcu():
            return False
        d = min(abs(delta), 63) | (0x40 if delta < 0 else 0)
        self._mcu_out.send_message([0xB0, 0x10 + strip, d])
        return True

    def _on_mcu(self, event, _data=None) -> None:
        msg, _dt = event
        with self._lock:
            out = self._mcu_out
            if out is None:                                 # closed while this message was in flight
                return
            self.mcu.last_rx = time.time(); self.mcu.rx_count += 1
            self.rx_log.append([hex(b) for b in msg]); del self.rx_log[:-50]
            st = msg[0] & 0xF0
            if st == 0x90 and len(msg) >= 3:
                n, v = msg[1], msg[2]
                if n in MCU_LED_NAMES:
                    self.mcu.leds[MCU_LED_NAMES[n]] = v > 0
                elif n < 0x20:  # per-strip LEDs: rec/solo/mute/select
                    kind = ("rec", "solo", "mute", "select")[n // 8]
                    self.mcu.strips.setdefault(n % 8, {})[kind] = v > 0
            elif st == 0xE0 and len(msg) >= 3:
                self.mcu.faders[msg[0] & 0x0F] = msg[1] | (msg[2] << 7)
            elif msg[0] == 0xF0 and len(msg) >= 7 and msg[1:4] == MCU_HDR:
                model, cmd = msg[4], msg[5]
                if cmd == 0x00 and model == MCU_MODEL:      # device query -> host connection query
                    out.send_message([0xF0, *MCU_HDR, MCU_MODEL, 0x01, *MCU_SERIAL, *MCU_CHALLENGE, 0xF7])
                elif cmd == 0x02 and model == MCU_MODEL:    # host connection reply -> confirmation
                    out.send_message([0xF0, *MCU_HDR, MCU_MODEL, 0x03, *MCU_SERIAL, 0xF7])
                    if not self.mcu.handshake:
                        self.mcu.handshake, self.mcu.handshake_at = True, time.time()
                elif cmd == 0x13 and model == MCU_MODEL:    # version request
                    out.send_message([0xF0, *MCU_HDR, MCU_MODEL, 0x14, *MCU_VERSION, 0xF7])
                elif cmd == 0x12 and len(msg) > 7:          # LCD text
                    if not self.mcu.handshake:
                        self.mcu.handshake, self.mcu.handshake_at = True, time.time()
                    pos = msg[6]
                    for i, b in enumerate(msg[7:-1]):
                        if 0 <= pos + i < 112:
                            self._lcd[pos + i] = chr(b) if 32 <= b < 127 else " "
                    self.mcu.lcd = "".join(self._lcd[:56]) + "\n" + "".join(self._lcd[56:])
            elif st == 0xB0 and len(msg) >= 3 and 0x40 <= msg[1] <= 0x4B:
                # 7-seg timecode display: CC 0x40..0x4B, value = char
                pass

    def close(self) -> None:
        with self._open_lock:
            ports = (self._mcu_in, self._mcu_out, self._out)
            for p in ports:  # input first (its close_port also cancels the callback): nothing new arrives mid-close
                try:
                    p and p.close_port()
                except Exception:
                    pass
            with self._lock:
                self._out = self._mcu_out = self._mcu_in = None
                # A reopened surface must handshake again; stale state would let mcu_button fire too early.
                self.mcu = McuState()
                self._lcd = [" "] * 112
                self.bank = None
            del ports, p  # last references: rtmidi destructors run here, not under the lock the callback needs
            self._release_mcu_lock()


_bus: MidiBus | None = None


def bus() -> MidiBus:
    global _bus
    if _bus is None:
        _bus = MidiBus()
    return _bus
