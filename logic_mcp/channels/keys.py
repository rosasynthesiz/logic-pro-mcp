"""CGEvent channel: post key commands straight to Logic's pid (no focus steal). Needs Accessibility trust."""
from __future__ import annotations

import time

import Quartz as Q

# macOS virtual key codes (US layout)
KEYCODES: dict[str, int] = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12,
    "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23,
    "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31, "u": 32, "[": 33, "i": 34,
    "p": 35, "return": 36, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42, ",": 43, "/": 44,
    "n": 45, "m": 46, ".": 47, "tab": 48, "space": 49, "`": 50, "delete": 51, "escape": 53,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101,
    "f10": 109, "f11": 103, "f12": 111, "home": 115, "end": 119, "pageup": 116, "pagedown": 121,
    "left": 123, "right": 124, "down": 125, "up": 126, "forwarddelete": 117,
    "kp_0": 82, "kp_1": 83, "kp_2": 84, "kp_3": 85, "kp_4": 86, "kp_5": 87, "kp_6": 88, "kp_7": 89,
    "kp_8": 91, "kp_9": 92, "kp_enter": 76, "kp_.": 65, "kp_*": 67, "kp_+": 69, "kp_/": 75, "kp_-": 78,
}
MODS = {
    "cmd": Q.kCGEventFlagMaskCommand,
    "shift": Q.kCGEventFlagMaskShift,
    "alt": Q.kCGEventFlagMaskAlternate,
    "opt": Q.kCGEventFlagMaskAlternate,
    "ctrl": Q.kCGEventFlagMaskControl,
}


def parse(chord: str) -> tuple[int, int]:
    """'cmd+shift+n' -> (keycode, flags)"""
    parts = [p.strip().lower() for p in chord.split("+")]
    key, mods = parts[-1], parts[:-1]
    if key not in KEYCODES:
        raise KeyError(f"unknown key {key!r}")
    flags = 0
    for m in mods:
        flags |= MODS[m]
    return KEYCODES[key], flags


def press(chord: str, pid: int | None = None, hold: float = 0.03) -> bool:
    code, flags = parse(chord)
    down = Q.CGEventCreateKeyboardEvent(None, code, True)
    up = Q.CGEventCreateKeyboardEvent(None, code, False)
    if down is None or up is None:
        return False
    Q.CGEventSetFlags(down, flags)
    Q.CGEventSetFlags(up, flags)
    if pid:
        Q.CGEventPostToPid(pid, down)
        time.sleep(hold)
        Q.CGEventPostToPid(pid, up)
    else:
        Q.CGEventPost(Q.kCGHIDEventTap, down)
        time.sleep(hold)
        Q.CGEventPost(Q.kCGHIDEventTap, up)
    return True


def type_text(text: str, pid: int | None = None) -> bool:
    for ch in text:
        ev_d = Q.CGEventCreateKeyboardEvent(None, 0, True)
        ev_u = Q.CGEventCreateKeyboardEvent(None, 0, False)
        Q.CGEventKeyboardSetUnicodeString(ev_d, len(ch), ch)
        Q.CGEventKeyboardSetUnicodeString(ev_u, len(ch), ch)
        if pid:
            Q.CGEventPostToPid(pid, ev_d); Q.CGEventPostToPid(pid, ev_u)
        else:
            Q.CGEventPost(Q.kCGHIDEventTap, ev_d); Q.CGEventPost(Q.kCGHIDEventTap, ev_u)
        time.sleep(0.01)
    return True


SHIFTED = {"!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9", ")": "0",
           "_": "-", "+": "=", "{": "[", "}": "]", ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`", "|": "\\"}


def type_keys(text: str, pid: int | None = None, delay: float = 0.02) -> bool:
    """Type via real key codes (works in fields that ignore unicode-string events). ASCII only."""
    for ch in text:
        if ch == " ":
            chord = "space"
        elif ch == "\n":
            chord = "return"
        elif ch.isalpha():
            chord = ("shift+" if ch.isupper() else "") + ch.lower()
        elif ch in SHIFTED:
            chord = "shift+" + SHIFTED[ch]
        elif ch in KEYCODES:
            chord = ch
        else:
            return False
        press(chord, pid, hold=0.015)
        time.sleep(delay)
    return True


# Logic Pro default key commands (US preset). Verify against Key Commands window; users can remap.
LOGIC_KEYS: dict[str, str] = {
    "play_stop": "space",
    "record": "r",
    "rewind_to_start": "return",
    "cycle": "c",
    "metronome": "k",
    "count_in": "shift+k",
    "new_software_instrument_track": "alt+cmd+s",
    "new_audio_track": "alt+cmd+a",
    "new_external_midi_track": "alt+cmd+x",
    "new_drummer_track": "alt+cmd+u",
    "delete_track": "cmd+delete",
    "mute_track": "m",
    "solo_track": "s",
    "record_enable_track": "ctrl+r",
    "undo": "cmd+z",
    "redo": "cmd+shift+z",
    "save": "cmd+s",
    "save_as": "cmd+shift+s",
    "new_project": "cmd+n",
    "open_project": "cmd+o",
    "close_project": "cmd+w",
    "bounce": "cmd+b",
    "mixer": "x",
    "piano_roll": "p",
    "score": "n",
    "library": "y",
    "inspector": "i",
    "automation": "a",
    "split_at_playhead": "cmd+t",
    "join": "cmd+j",
    "quantize": "q",
    "select_all": "cmd+a",
    "go_to_position": "/",
    "zoom_to_fit": "z",
    "create_marker": "alt+'",
    "step_input": "alt+cmd+i",
    "track_up": "up",
    "track_down": "down",
}
