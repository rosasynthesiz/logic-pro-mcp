"""logic-mcp server: dispatcher tools + live-state resources over stdio."""
from __future__ import annotations

import json
import time
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer

from . import (audio as audiolib, doctor, export, leveling, markers, plugins as plugs, projectdata, target,
               upload as uploader)
from .channels import applescript, ax, ax_logic, dialogs, keys, osc, palette, screen
from .channels.midi import MCU_BUTTONS, MMC, bus
from .result import fail, ok, unsure

INSTRUCTIONS = """Drive Logic Pro on this Mac. Rules:
- Call logic_system(action='health') first. Only channels listed in `ready` are trustworthy.
- Mutating tools return state=confirmed|uncertain|failed. Only `confirmed` means it happened. On `uncertain`, read a resource and verify before retrying.
- Read logic://transport before changing transport. Never assume state.
- If a channel is blocked, the response carries `fix`. Report it to the human instead of retrying.
"""

mcp = MCPServer("logic-mcp", instructions=INSTRUCTIONS)


def _app():
    return target.find()


def _pid() -> int | None:
    a = _app()
    return a.pid if a and a.running else None


def _mcu_live(max_age: float = 30.0) -> bool:
    b = bus()
    if not b.mcu.handshake:
        b.wait_ready(timeout=3.0)
    return b.mcu.handshake and (time.time() - b.mcu.last_rx) < max_age


# --------------------------------------------------------------------------- readback
def _win():
    pid = _pid()
    if not pid or not ax.trusted():
        return None
    return ax_logic.main_window(ax.app(pid))


def _why_no_win() -> tuple[str, str]:
    """(reason, fix) for a missing main window — distinguishes locked screen from missing permission from no project."""
    s = doctor.session_state()
    if not s["ok"]:
        return "screen locked or display asleep: Logic's UI is invisible to Accessibility", "unlock the Mac / wake the display"
    if not ax.trusted():
        return "Accessibility not trusted", "System Settings > Privacy & Security > Accessibility"
    if not _pid():
        return "Logic not running", "logic_project(action='launch')"
    return "no project window open in Logic", "open or create a project"


def transport_state() -> dict:
    out: dict = {"sources": []}
    b = bus()
    if _mcu_live():
        out["mcu"] = b.mcu.to_dict(); out["sources"].append("mcu")
    w = _win()
    if w is not None:
        out["ax"] = ax_logic.transport_snapshot(w); out["sources"].append("ax")
    mcu, axs = out.get("mcu", {}), out.get("ax", {})
    out["playing"] = mcu.get("playing") if "mcu" in out else axs.get("play")
    out["recording"] = mcu.get("recording") if "mcu" in out else axs.get("record")
    out["tempo"] = axs.get("tempo"); out["position"] = axs.get("position"); out["cycle"] = axs.get("cycle")
    if not out["sources"]:
        out["reason"] = "no MCU handshake and Accessibility not trusted"
    return out


def _playing() -> bool | None:
    return transport_state().get("playing")


# --------------------------------------------------------------------------- tools
@mcp.tool()
def logic_system(
    action: Literal["health", "permissions", "midi_ports", "ax_dump", "ax_find", "key_map", "app", "menu", "press_menu", "windows"],
    depth: int = 5,
    max_nodes: int = 300,
    contains: str | None = None,
    role: str | None = None,
    window: int = 0,
    path: list[str] | None = None,
) -> dict:
    """Health, permissions, MIDI ports, menu bar (menu/press_menu with path like ['Track','New Audio Track']),
    and Accessibility exploration (ax_dump / ax_find) of Logic's UI."""
    if action == "health":
        return doctor.run()
    if action in ("menu", "press_menu", "windows"):
        if not ax.trusted() or not (pid := _pid()):
            return fail("ax", "needs Accessibility + running Logic")
        root = ax.app(pid)
        if action == "windows":
            return {"windows": ax_logic.window_titles(root)}
        if action == "menu":
            return {"path": path or [], "items": ax_logic.menu_items(root, *(path or []))}
        if not path:
            return fail("menu", "need path")
        return ok("menu", f"pressed {' > '.join(path)}", {"windows": ax_logic.window_titles(root)}) if ax_logic.press_menu_item(root, *path) else fail("menu", f"item not found: {path}")
    if action == "permissions":
        return {"accessibility": ax.trusted(), "automation_system_events": applescript.frontmost() is not None}
    if action == "midi_ports":
        return bus().list_ports()
    if action == "app":
        a = _app()
        return a.to_dict() if a else {"error": "Logic not found"}
    if action == "key_map":
        return keys.LOGIC_KEYS
    if not ax.trusted():
        return fail("ax", "Accessibility not trusted", "System Settings > Privacy & Security > Accessibility")
    pid = _pid()
    if not pid:
        return fail("ax", "Logic not running")
    root = ax.app(pid)
    wins = ax.windows(root)
    node = wins[window] if wins and window < len(wins) else root
    if action == "ax_dump":
        return {"windows": [ax.info(w)["title"] for w in wins], "nodes": ax.dump(node, depth, max_nodes, {role} if role else None)}
    if action == "ax_find":
        hits = ax.find_all(node, lambda e: (not role or ax.role(e) == role) and (not contains or contains.lower() in " ".join(x for x in (ax.info(e)["title"], ax.info(e)["desc"], ax.info(e)["id"]) if x).lower()), max_depth=depth + 20, limit=50)
        return {"count": len(hits), "hits": [ax.info(h) | {"actions": ax.actions(h)} for h in hits]}
    return fail("system", f"unknown action {action}")


class _front:
    """Bring Logic forward only for the duration of a key send, then hand focus back to whatever the human was using."""
    def __enter__(self):
        self.prev = applescript.frontmost()
        a = _app(); self.logic = a.name if a else None
        if self.logic and self.prev != self.logic:
            applescript.activate(self.logic); time.sleep(0.2)
        return self
    def __exit__(self, *_):
        time.sleep(0.15)
        if self.prev and self.logic and self.prev != self.logic:
            applescript.activate(self.prev)


def _key(command: str) -> bool:
    a = _app()
    if not a or not a.running or not ax.trusted():
        return False
    with _front():
        return keys.press(keys.LOGIC_KEYS[command], a.pid)


@mcp.tool()
def logic_transport(
    action: Literal["play", "stop", "record", "go_to_beginning", "rewind", "fast_forward", "toggle_cycle", "toggle_count_in",
                    "toggle_metronome", "set_tempo", "goto", "state"],
    tempo: float | None = None,
    bar: int | None = None,
    beat: int = 1,
    division: int = 1,
    tick: int = 1,
) -> dict:
    """Transport with verified readback (MCU LEDs + Accessibility). play/record via AX press; stop via MCU or Space."""
    if action == "state":
        return transport_state()
    b = bus(); w = _win()

    if action in ("play", "record"):
        field = "recording" if action == "record" else "playing"
        s = transport_state()
        if s.get(field) is True:
            return ok("readback", f"already {field}", s)
        if s.get(field) is not False:
            return unsure("readback", f"{field} state unknown; refusing to toggle {action}", s)
        if w is not None and (el := ax_logic.bar_control(w, action)) is not None:
            r = ax_logic.set_checkbox(el, True); time.sleep(0.3); s = transport_state()
            good = s.get(field)
            return ok("ax", f"{action} via {r['how']}", s) if good is True else unsure("ax", f"pressed {action}, readback={good}", s)
        if _mcu_live() and b.mcu_button(action):
            time.sleep(0.5); s = transport_state()
            return ok("mcu", action, s) if s.get(field) is True else unsure("mcu", f"button sent, {field} not confirmed", s)
        if _key("play_stop" if action == "play" else "record"):
            time.sleep(0.5); s = transport_state()
            return ok("keys", action, s) if s.get(field) is True else unsure("keys", f"key sent, {field} not confirmed", s)
        return fail("transport", "no channel available", "grant Accessibility or wait for MCU handshake")

    if action == "stop":
        s = transport_state()
        if s.get("playing") is False:
            return ok("readback", "already stopped", s)
        if _mcu_live() and b.mcu_button("stop"):
            time.sleep(0.5); s = transport_state()
            return ok("mcu", "stop", s) if s.get("playing") is False else unsure("mcu", "stop sent, stopped state not confirmed", s)
        if s.get("playing") is True and _key("play_stop"):
            time.sleep(0.5); s = transport_state()
            return ok("keys", "stop", s) if s.get("playing") is False else unsure("keys", "space sent, still playing?", s)
        return unsure("mmc", "MMC stop sent (needs 'Listen to MMC')") if b.mmc("stop") else fail("transport", "no channel")

    if action == "go_to_beginning":
        el = None
        for _ in range(3):  # AX tree occasionally misses the button right after a dialog closes
            el = ax_logic.bar_control(w, "Go to Beginning") if w is not None else None
            if el is not None:
                break
            time.sleep(0.2)
        if el is not None and ax.press(el):
            time.sleep(0.3); s = transport_state(); pos = (s.get("position") or {}).get("bar")
            return ok("ax", "go to beginning", s) if pos == 1.0 else unsure("ax", f"bar={pos}", s)
        if _key("rewind_to_start"):
            time.sleep(0.3); s = transport_state(); pos = (s.get("position") or {}).get("bar")
            return ok("keys", "go to beginning", s) if pos == 1.0 else unsure("keys", f"Return sent, bar={pos}", s)
        return fail("transport", "no channel")

    if action in ("rewind", "fast_forward"):
        if _mcu_live() and b.mcu_button(action):
            return unsure("mcu", f"{action} button sent; MCU rewind is a nudge, read position", transport_state())
        return fail("transport", "needs MCU surface")

    if action in ("toggle_cycle", "toggle_count_in"):
        key = action.removeprefix("toggle_")
        if w is not None and (el := ax_logic.bar_control(w, key)) is not None:
            cur = ax_logic._truthy(ax.value(el)); r = ax_logic.set_checkbox(el, not bool(cur))
            return ok("ax", f"{key} -> {r['after']}", r) if r["ok"] else unsure("ax", f"{key} readback {r['after']}", r)
        return unsure("keys", "key sent") if _key(key) else fail("transport", "no channel")

    if action == "toggle_metronome":
        if _mcu_live() and b.mcu_button("click"):
            time.sleep(0.3); return unsure("mcu", "click button sent", {"click_led": b.mcu.leds.get("click")})
        return unsure("keys", "K sent") if _key("metronome") else fail("transport", "no channel")

    if action == "set_tempo":
        if tempo is None or not 5 <= tempo <= 990:
            return fail("ax", "tempo must be 5..990")
        if w is None or (el := ax_logic.tempo_slider(w)) is None:
            return fail("ax", "tempo slider not found (Accessibility?)")
        r = ax_logic.set_slider(el, tempo)
        return ok("ax", f"tempo {r['before']} -> {r['after']}", r) if r["ok"] else unsure("ax", f"tempo readback {r['after']}", r)

    if action == "goto":
        if bar is None or w is None:
            return fail("ax", "need bar (and Accessibility)")
        root = ax.app(_pid())
        with _front():
            if not ax_logic.press_menu_item(root, "Navigate", "Go To", "~Position"):
                return fail("menu", "Navigate > Go To > Position… not found", {"items": ax_logic.menu_items(root, "Navigate", "Go To")})
            dlg = ax_logic.wait_window(root, "Go To Position")
            if dlg is None:
                return unsure("menu", "dialog did not appear", {"windows": ax_logic.window_titles(root)})
            # The dialog's segments ignore AX writes; the "New" bar field has keyboard focus, so type digits + Return.
            pid = _pid()
            keys.type_keys(f"{bar} {beat} {division} {tick}", pid); time.sleep(0.1); keys.press("return", pid)
            time.sleep(0.5)
            if ax_logic.find_window(root, "Go To Position") is not None:
                ax_logic.close_window(root, "Go To Position")
        s = transport_state(); pos = s.get("position") or {}
        return ok("menu+ax", f"playhead -> {pos}", s) if pos.get("bar") == float(bar) and pos.get("beat") == float(beat) else unsure("menu+ax", f"position now {pos}", s)

    return fail("transport", f"unknown action {action}")


@mcp.tool()
def logic_tracks(
    action: Literal["list", "select", "mute", "solo", "arm", "monitor", "set_volume", "set_pan", "set_color", "get_color",
                    "create_instrument", "create_audio", "create_drummer", "create_external_midi", "delete", "rename", "palette"],
    index: int | None = None,
    name: str | None = None,
    on: bool = True,
    value: float | None = None,
    db: float | None = None,
    new_name: str | None = None,
    color: str | None = None,
    colors: bool = False,
) -> dict:
    """Track list + per-track ops. Target by index (1-based) or name. Writes go via MCU (mute/solo/arm/select/volume)
    with Accessibility readback, or AX nudging as fallback. set_volume takes db (-inf..+6) or raw value 0-233 (173 = 0 dB);
    set_pan takes value 0-127 (64 = center). set_color takes color = 1..96 | '#rrggbb' | 'blue' | 'dark green' | 'light rose'
    (see action='palette'); verified by the palette's selection ring. list with colors=true samples header icon tints (approx)."""
    w = _win()
    if w is None:
        return fail("ax", *_why_no_win())
    if action == "list":
        tracks = ax_logic.list_tracks(w, colors=colors, logic_pid=_pid())
        if colors:
            for t in tracks:
                c = t.get("color")
                if c:
                    c["approx_swatch"] = palette.nearest_by_hue(c["hex"]); c["approx_name"] = palette.name_of(c["approx_swatch"])
        return {"tracks": tracks}
    if action == "palette":
        return {"columns": palette.COLUMN_NAMES, "rows": palette.ROW_NAMES, "aliases": palette.ALIASES,
                "swatches": [{"index": i, "name": palette.name_of(i), "hex": palette.hex_of(i)} for i in range(1, 97)]}

    root = ax.app(_pid())
    if action.startswith("create_"):
        item = {"create_instrument": "New Software Instrument Track", "create_audio": "New Audio Track",
                "create_external_midi": "New External MIDI Track", "create_drummer": "~^New Session Player"}[action]
        before = [t["name"] for t in ax_logic.list_tracks(w)]
        if not ax_logic.press_menu_item(root, "Track", item):
            return fail("menu", f"Track > {item} not found")
        for _ in range(25):
            time.sleep(0.15)
            if len(ax_logic.track_items(w)) > len(before):
                break
        tracks = ax_logic.list_tracks(w)
        new = [t for t in tracks if t["name"] not in before] or ([tracks[-1]] if len(tracks) > len(before) else [])
        if len(tracks) > len(before):
            return ok("menu", f"{action}: {len(before)} -> {len(tracks)} tracks", new[0] if new else None)
        return unsure("menu", "track count unchanged; a dialog may be open", {"windows": ax_logic.window_titles(root)})

    if index is None and name is None:
        return fail("ax", "need index or name (fail-closed for track ops)")
    item = ax_logic.find_track(w, index, name, fuzzy=action not in ("delete", "rename", "set_color"))
    if item is None:
        return fail("ax", f"track not found: index={index} name={name!r}", "call logic_tracks(action='list')")
    info = ax_logic.track_info(item)

    b = bus()
    all_names = [t["name"] for t in tracks_now] if (tracks_now := ax_logic.list_tracks(w)) else []
    if action == "select":
        if info.get("selected"):
            return ok("readback", f"{info['name']} already selected", info)
        if _mcu_live() and b.mcu_select_track(info["index"], all_names):
            time.sleep(0.3)
            after = ax_logic.track_info(item)
            if after.get("selected"):
                return ok("mcu", f"selected {after['name']}", after)
        ax_logic.select_track(item); time.sleep(0.25); after = ax_logic.track_info(item)
        return ok("ax", f"selected {after['name']}", after) if after.get("selected") else unsure("ax", "focus not confirmed", after)

    if action in ("mute", "solo", "arm", "monitor"):
        if info.get(action) == on:
            return ok("readback", f"{info['name']} {action} already {on}", info)

        def _settled(target, tries: int = 6, step: float = 0.25):
            """Re-resolve the track each time: `item` is an AX element captured before the write, and a
            single read through it can report the pre-write value (or a stale one) and turn a failed
            toggle into a false 'confirmed' -- measured on 2026-09-08, mute reported True while Logic
            had it off."""
            cur = None
            for _ in range(tries):
                time.sleep(step)
                fresh = ax_logic.find_track(w, info["index"])
                if fresh is None:
                    continue
                cur = ax_logic.track_info(fresh).get(action)
                if cur == target:
                    return True, cur
            return False, cur

        strip = b.strip_for(info["index"], all_names) if (action != "monitor" and _mcu_live()) else None
        if strip is not None:
            b.mcu_strip({"mute": "mute", "solo": "solo", "arm": "rec"}[action], strip)
            good, cur = _settled(on)
            if good:
                return ok("mcu", f"{info['name']} {action}={on}", ax_logic.track_info(ax_logic.find_track(w, info["index"])))
        el = ax_logic.track_toggle_el(item, action)
        if el is None:
            return fail("ax", f"{action} control not found on track")
        r = ax_logic.set_checkbox(el, on)
        good, cur = _settled(on)
        if good:
            return ok("ax", f"{info['name']} {action}={on}", r | {"verified": cur})
        return unsure("ax", f"{action} not applied: Logic still reads {action}={cur}", r | {"verified": cur},
                      fix="logic_osc(action='mute'|'solo', strip=N) drives this reliably in both directions "
                          "when the track is in the current 8-strip bank; the header control can also be "
                          "hidden by the Configure Track Header settings")

    if action == "set_volume":
        vol_el = ax.find(item, role_="AXSlider", desc_="Volume", max_depth=1)
        strip = b.strip_for(info["index"], all_names) if (db is not None and _mcu_live()) else None
        if strip is not None and vol_el is not None:
            lo, hi = 0, 16383; guess = 12443 if db >= -6 else 6000
            best = None
            for _ in range(9):  # binary search on Logic's own dB readback
                b.mcu_fader(strip, guess); time.sleep(0.25)
                got = ax_logic.parse_db(ax.attr(vol_el, "AXValueDescription"))
                best = (guess, got)
                if got is None or abs(got - db) <= 0.25:
                    break
                lo, hi = (guess, hi) if got < db else (lo, guess)
                guess = (lo + hi) // 2
            return ok("mcu", f"{info['name']} volume -> {best[1]} dB (fader {best[0]})", ax_logic.track_info(item)) if best and best[1] is not None and abs(best[1] - db) <= 0.25 \
                else unsure("mcu", f"closest {best[1] if best else None} dB", ax_logic.track_info(item))
        if db is not None and value is None:      # MCU could not reach it — nudge the AX slider to the dB
            if vol_el is None:
                return fail("ax", "volume slider not found")
            tgt_raw = None
            r = ax_logic.nudge_slider_to_db(vol_el, db)
            return ok("ax", f"{info['name']} volume -> {r['display']}", r) if r["ok"] \
                else unsure("ax", f"landed at {r['display']}", r)
        if value is None:
            return fail("ax", "need db or raw value (0-233, 173 = 0 dB)")
        if vol_el is None:
            return fail("ax", "volume slider not found")
        r = ax_logic.nudge_slider(vol_el, value)
        return ok("ax", f"{info['name']} volume {r['before']} -> {r['after']} ({ax.attr(vol_el, 'AXValueDescription')})", r) if r["ok"] else unsure("ax", f"readback {r['after']}", r)

    if action == "set_pan":
        if value is None:
            return fail("ax", "need value 0-127 (64 = center)")
        # OSC first: the header pan slider only supports Increment/Decrement and settles within ~4 of 127,
        # so the AX path could never honestly report `confirmed`. Over OSC the readback is Logic's own
        # value. Only strips currently on the surface can be addressed, so AX stays as the fallback.
        strip = _osc_strip_for(index)
        if strip is not None:
            r = logic_osc("pan", strip=strip, value=max(0.0, min(1.0, value / 127.0)))
            if r.get("state") == "confirmed":
                return r | {"track": info["name"], "channel": "osc"}
        el = ax_logic._pan_slider(item)
        if el is None:
            return fail("ax", "pan slider not found")
        r = ax_logic.nudge_slider(el, value)
        return ok("ax", f"{info['name']} pan {r['before']} -> {r['after']}", r) if r["ok"] else unsure(
            "ax", f"readback {r['after']} (header slider settles within ~4 of 127; "
                  "put the track on the OSC surface bank for an exact, confirmable move)", r)

    if action in ("set_color", "get_color"):
        target_idx = None
        if action == "set_color":
            if color is None:
                return fail("palette", "need color (1..96, '#rrggbb', 'blue', 'dark green'...)", "logic_tracks(action='palette')")
            try:
                target_idx = palette.parse(color)
            except ValueError as e:
                return fail("palette", str(e))
        if not screen.permitted():
            return fail("screen", "Screen Recording not granted: the palette selection ring cannot be read back",
                        "System Settings > Privacy & Security > Screen Recording (grant to the process running logic-mcp)")
        # palette acts on the selected track(s): make the target the only selection, then ask Logic itself how many are selected
        logic_tracks("select", index=info["index"])
        selected = [t["index"] for t in ax_logic.list_tracks(w) if t["selected"]]
        if selected != [info["index"]]:
            return fail("ax", f"refusing {action}: focused tracks are {selected}, expected [{info['index']}]")
        cnt = ax_logic.selection_count(root)
        if cnt != 1:
            return fail("menu", f"refusing {action}: Logic reports {cnt} selected tracks", "click a single track in Logic first")
        pid = _pid()
        with _front():
            pal = palette.open_palette(root)
            if pal is None:
                return fail("palette", "Color window did not open", {"windows": ax_logic.window_titles(root)})
            closed = False
            try:
                kids = palette.swatches(pal)
                if len(kids) < 97:
                    return fail("palette", f"expected 97 palette children, got {len(kids)}")
                before = palette.selected_index(pal, kids)
                if action == "get_color":
                    return ok("palette", f"{info['name']} color = {palette.name_of(before) if before else None}",
                              {"index": before, "name": palette.name_of(before) if before else None, "hex": palette.hex_of(before) if before else None}) \
                        if before else unsure("palette", "no selection ring found (capture failed or palette not visible)", {"index": None})
                if before == target_idx:
                    return ok("palette", f"{info['name']} already {palette.name_of(target_idx)}", {"index": before, "hex": palette.hex_of(before)})
                # AXPress is inert on swatches (measured). Real click: pid-posted first, HID click if the ring did not move.
                how = "click:pid"
                palette.click_swatch(kids, target_idx, pid=pid); time.sleep(0.35)
                after = palette.selected_index(pal, kids)
                if after != target_idx:
                    how = "click:hid"
                    palette.click_swatch(kids, target_idx, pid=None); time.sleep(0.35)
                    after = palette.selected_index(pal, kids)
                rb = {"before": before, "after": after, "requested": target_idx, "name": palette.name_of(target_idx), "hex": palette.hex_of(target_idx), "how": how}
                if after == target_idx:
                    return ok("palette", f"{info['name']} color -> {palette.name_of(target_idx)} (#{palette.hex_of(target_idx)})", rb)
                return unsure("palette", f"clicked swatch {target_idx}, ring now at {after}", rb)
            finally:
                closed = palette.close_palette(root)
                if not closed:
                    keys.press("escape", pid)
    if action in ("delete", "rename"):
        # fail-closed: the target must be the ONLY selected track, and Logic's own menu must say "1 Track"
        sel = logic_tracks("select", index=info["index"])
        selected = [t["index"] for t in ax_logic.list_tracks(w) if t["selected"]]
        if selected != [info["index"]]:
            return fail("ax", f"refusing {action}: selection is {selected}, expected [{info['index']}]", "select the track first")
        if action == "rename" and not new_name:
            return fail("ax", "need new_name")
        verb = "Delete" if action == "delete" else "Rename"
        n0 = len(ax_logic.track_items(w))
        with _front():
            # Menu titles only re-validate when the menu opens: "Delete Track" (1 selected) vs "Delete 8 Tracks".
            pressed, titles = ax_logic.press_fresh_menu_item(root, "Track", rf"^{verb} (Track|1 Track)$")
            if not pressed:
                return fail("menu", f"Logic says {[t for t in titles if t.startswith(verb)]}; refusing (selection is not exactly 1 track)")
            time.sleep(0.5)
            if action == "delete":
                n1 = len(ax_logic.track_items(w))
                return ok("menu", f"deleted {info['name']}: {n0} -> {n1}") if n1 == n0 - 1 else unsure("menu", f"count {n0} -> {n1}; confirm dialog?", {"windows": ax_logic.window_titles(root)})
            keys.press("cmd+a", _pid()); time.sleep(0.05)
            keys.type_keys(new_name, _pid()) or keys.type_text(new_name, _pid())
            time.sleep(0.1); keys.press("return", _pid()); time.sleep(0.5)
        after = ax_logic.track_info(ax_logic.find_track(w, info["index"]))
        return ok("menu", f"renamed {info['name']!r} -> {after['name']!r}", after) if after["name"] == new_name else unsure("menu", f"name now {after['name']!r}", after)
    return fail("tracks", f"unknown action {action}")


@mcp.tool()
def logic_midi(
    action: Literal["note", "chord", "sequence", "cc", "program", "pitch_bend", "aftertouch", "sysex", "mmc", "ports", "mcu_button", "mcu_strip", "mcu_fader"],
    note: int | None = None,
    notes: list[int] | None = None,
    velocity: int = 100,
    channel: int = 1,
    duration: float = 0.3,
    steps: list[dict[str, Any]] | None = None,
    bpm: float = 120.0,
    number: int | None = None,
    value: int | None = None,
    data: list[int] | None = None,
    command: str | None = None,
    strip: int = 0,
) -> dict:
    """Send MIDI into Logic's Virtual In (lands on the record-armed track) or drive the MCU surface."""
    b = bus()
    if action == "ports":
        return b.list_ports()
    if action in ("note", "chord", "sequence", "cc", "program", "pitch_bend", "aftertouch", "sysex", "mmc") and not b.open_logic_in():
        return fail("midi", "'Logic Pro Virtual In' port not found", "Start Logic Pro")
    if action == "note" and note is not None:
        return ok("midi", f"note {note} vel {velocity} ch {channel}") if b.note(note, velocity, channel, duration) else fail("midi", "send failed")
    if action == "chord" and notes:
        return ok("midi", f"chord {notes}") if b.chord(notes, velocity, channel, duration) else fail("midi", "send failed")
    if action == "sequence" and steps:
        b.sequence(steps, bpm, channel)
        return ok("midi", f"played {len(steps)} steps at {bpm} bpm")
    if action == "cc" and number is not None and value is not None:
        return ok("midi", f"cc{number}={value}") if b.cc(number, value, channel) else fail("midi", "send failed")
    if action == "program" and number is not None:
        return ok("midi", f"program {number}") if b.program(number, channel) else fail("midi", "send failed")
    if action == "pitch_bend" and value is not None:
        return ok("midi", f"pitch bend {value}") if b.pitch_bend(value, channel) else fail("midi", "send failed")
    if action == "aftertouch" and value is not None:
        return ok("midi", f"aftertouch {value}") if b.aftertouch(value, channel) else fail("midi", "send failed")
    if action == "sysex" and data:
        return ok("midi", f"sysex {len(data)} bytes") if b.sysex(data) else fail("midi", "send failed")
    if action == "mmc" and command in MMC:
        return unsure("midi", f"MMC {command} sent; Logic must have 'Listen to MMC Input' enabled") if b.mmc(command) else fail("midi", "send failed")
    if action == "mcu_button" and command in MCU_BUTTONS:
        b.open_mcu(); b.mcu_button(command); time.sleep(0.2)
        return (ok if _mcu_live() else unsure)("mcu", f"button {command}", b.mcu.to_dict()) if _mcu_live() else unsure("mcu", "no MCU traffic from Logic; surface not configured", b.mcu.to_dict())
    if action == "mcu_strip" and command:
        b.open_mcu(); return ok("mcu", f"{command} strip {strip}") if b.mcu_strip(command, strip) else fail("mcu", "bad strip/kind")
    if action == "mcu_fader" and value is not None:
        b.open_mcu(); return ok("mcu", f"fader {strip}={value}") if b.mcu_fader(strip, value) else fail("mcu", "send failed")
    return fail("midi", f"bad args for {action}")


@mcp.tool()
def logic_project(
    action: Literal["launch", "activate", "quit", "open", "save", "save_as", "new", "close", "undo", "redo", "info"],
    path: str | None = None,
) -> dict:
    """Project lifecycle via AppleScript + key commands. save/new/close use key commands (need Accessibility)."""
    a = _app()
    if a is None:
        return fail("applescript", "Logic not installed")
    if action == "info":
        return a.to_dict() | {"frontmost": applescript.frontmost()}
    if action == "launch" or action == "activate":
        okk, out = applescript.activate(a.name)
        time.sleep(1.0 if action == "launch" else 0.2)
        return ok("applescript", out or action, target.find().to_dict()) if okk else fail("applescript", out)
    if action == "quit":
        okk, out = applescript.quit_app(a.name)
        return ok("applescript", "quit sent") if okk else fail("applescript", out)
    if action == "open" and path:
        okk, out = applescript.open_file(a.name, path)
        return unsure("applescript", "open sent; verify window title") if okk else fail("applescript", out)
    if action in ("save", "save_as", "new", "close", "undo", "redo"):
        if not ax.trusted() or not a.running:
            return fail("keys", "needs Accessibility + running Logic")
        chord = {"save": "save", "save_as": "save_as", "new": "new_project", "close": "close_project", "undo": "undo", "redo": "redo"}[action]
        _key(chord)
        time.sleep(0.4)
        return unsure("keys", f"{action} key command sent; a dialog may be open", {"windows": ax_logic.window_titles(ax.app(a.pid))})
    return fail("project", f"bad args for {action}")


@mcp.tool()
def logic_offline(
    action: Literal["list", "rename"],
    project: str,
    changes: list[dict] | None = None,
    in_place: bool = False,
) -> dict:
    """Read or rename track/folder/group names INSIDE a .logicx file, without driving Logic's UI.

    list:   every patchable name in the project (kind: track | folder | group), plus counts.
    rename: apply changes [{"old","new","kind"?}] to the project's ProjectData. By default writes to an
            APFS clone "<name> OFFLINE.logicx" next to the original (instant, no data copied); in_place
            patches the original after saving ProjectData.bak, and is refused while Logic has that
            document open (Logic would overwrite it on save). Returns uncertain: only Logic reopening
            the file proves the edit, so open the result and read the names back.
    """
    import datetime
    import os
    import shutil
    import subprocess
    from pathlib import Path

    bundle = Path(os.path.expanduser(project))
    pdata = bundle / "Alternatives" / "000" / "ProjectData"
    if not pdata.exists():
        return fail("offline", f"no ProjectData under {bundle}")
    data = pdata.read_bytes()
    if action == "list":
        recs = [r for r in projectdata.records(data) if r["kind"] in ("track", "folder", "group")]
        return ok("offline", f"{len(recs)} names", {
            "counts": {k: sum(1 for r in recs if r["kind"] == k) for k in ("track", "folder", "group")},
            "names": [{"kind": r["kind"], "name": r["name"]} for r in recs],
            "layout_problems": projectdata.check_layout(data)})
    if not changes:
        return fail("offline", "rename needs changes=[{old,new,kind?}]")
    try:
        patched = projectdata.rename_many(data, [(c["old"], c["new"], c.get("kind")) for c in changes])
    except (ValueError, TypeError, KeyError) as e:
        return fail("offline", str(e))
    if in_place:
        a = _app()
        w = _win()
        if w is not None:
            doc = str(ax.attr(w, "AXDocument") or "")
            if bundle.name in doc:
                return fail("offline", "Logic has this project open; close it first or omit in_place",
                            "Logic rewrites ProjectData on save, which would discard the patch")
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        shutil.copy2(pdata, pdata.with_name(f"ProjectData.bak-{stamp}"))
        target_bundle = bundle
    else:
        target_bundle = bundle.with_name(bundle.stem + " OFFLINE.logicx")
        if target_bundle.exists():
            return fail("offline", f"{target_bundle.name} already exists; remove or rename it first")
        r = subprocess.run(["cp", "-Rc", str(bundle), str(target_bundle)], capture_output=True, text=True)
        if r.returncode != 0:
            return fail("offline", f"clone failed: {r.stderr.strip()[:200]}")
        # The clone must not inherit the original's autosave or undo state: with them present Logic offers
        # to open the (older) auto-saved version instead of the patched file, and picking it discards the
        # patch silently. Measured on the first full-bundle test.
        for stale in ("Autosave", "Undo Data.nosync"):
            shutil.rmtree(target_bundle / "Alternatives" / "000" / stale, ignore_errors=True)
    out = target_bundle / "Alternatives" / "000" / "ProjectData"
    tmp = out.with_suffix(".tmp")
    tmp.write_bytes(patched)
    os.replace(tmp, out)
    after = projectdata.records(patched)
    applied = [c["new"] for c in changes if any(r["name"] == c["new"] and r["kind"] != "other" for r in after)]
    return unsure("offline", f"{len(applied)}/{len(changes)} renames written to {target_bundle.name}", {
        "bundle": str(target_bundle), "applied": applied,
        "verify": "open this project in Logic (close + open, not Revert) and read the names back"})


@mcp.tool()
def logic_keys(chord: str | None = None, command: str | None = None) -> dict:
    """Escape hatch: send any key chord ('cmd+shift+n') or a named Logic command from key_map to Logic."""
    a = _app()
    if not a or not a.running:
        return fail("keys", "Logic not running")
    if not ax.trusted():
        return fail("keys", "Accessibility not trusted")
    if command:
        if command not in keys.LOGIC_KEYS:
            return fail("keys", f"unknown command {command}; see logic_system(action='key_map')")
        chord = keys.LOGIC_KEYS[command]
    if not chord:
        return fail("keys", "need chord or command")
    with _front():
        sent = keys.press(chord, a.pid)
    return unsure("keys", f"sent {chord}; verify via resource") if sent else fail("keys", "post failed")


@mcp.tool()
def logic_export(
    action: Literal["bounce", "stems", "options", "dismiss"],
    directory: str | None = None,
    filename: str | None = None,
    formats: list[str] | None = None,
    start_bar: int | None = None,
    end_bar: int | None = None,
    sample_rate: str | None = None,
    bit_depth: str | None = None,
    normalize: str | None = None,
    mode: str | None = None,
    include_tail: bool | None = None,
    timeout: float = 900.0,
) -> dict:
    """Render audio out of Logic and verify it landed on disk.

    bounce  - one pass, several formats at once: formats=["wav","mp3"]. Optional start_bar/end_bar limit the range.
              sample_rate like "44.1 kHz", bit_depth like "24-bit", normalize "Off", mode "Offline".
    stems   - File > Export > All Tracks as Audio Files, one file per track (the input for level analysis).
    options - read the current bounce dialog settings without changing anything.
    dismiss - close any stray Logic dialog (escape hatch).
    """
    if action in ("bounce", "stems") and not directory:
        # Rosa's rule: every output lands in ~/Desktop/<project name>/Bounces/<date>/ so anyone can find it.
        import datetime, os
        w = _win()
        doc = str(ax.attr(w, "AXDocument") or "") if w is not None else ""
        proj = os.path.splitext(os.path.basename(doc.rstrip("/")))[0] if doc else "Logic Output"
        proj = proj.replace("%20", " ")
        directory = os.path.expanduser(f"~/Desktop/{proj}/Bounces/{datetime.date.today().isoformat()}")
        os.makedirs(directory, exist_ok=True)
        if not filename:
            filename = proj

    a = _app()
    if a is None or not a.running:
        return fail("export", "Logic is not running")
    s = doctor.session_state()
    if not s["ok"]:
        return fail("export", "screen locked or display asleep", "unlock the Mac")
    if not ax.trusted():
        return fail("export", "Accessibility not trusted", "System Settings > Privacy & Security > Accessibility")
    root = ax.app(a.pid)

    if action == "dismiss":
        acted = dialogs.cancel_all(root)
        return ok("dialogs", f"dismissed {acted}" if acted else "nothing to dismiss", {"windows": ax_logic.window_titles(root)})

    if action == "options":
        if dialogs.other_windows(root):
            return fail("export", "another dialog is already open", {"windows": ax_logic.window_titles(root)})
        with _front():
            dlg = export.open_bounce(root)
            if dlg is None:
                return fail("menu", "Bounce dialog did not open")
            try:
                out = {"formats": {}, "popups": {}, "range": {}}
                for lbl in ("Uncompressed", "MP3", "M4A"):
                    cb = export._fmt_checkbox(dlg, lbl)
                    out["formats"][lbl] = ax_logic._truthy(ax.value(cb)) if cb is not None else None
                # Options are addressed by visible label, never by AXIdentifier: the right-hand panel swaps
                # with the selected format row and the identifiers are reused across panels.
                selected = next((k for k, v in export.format_rows(dlg).items() if v), None)
                out["selected_format"] = selected
                for lbl in export.ROW_OPTIONS.get(selected, ()) + export.SHARED_OPTIONS:
                    el = dialogs.popup_for_label(dlg, lbl)
                    out["popups"][lbl.strip(":").lower().replace(" ", "_")] = dialogs.popup_value(el) if el is not None else None
                for lbl in ("Start:", "End:"):
                    g = dialogs.segment_group(dlg, lbl)
                    out["range"][lbl.strip(":").lower()] = dialogs.segment_values(g) if g is not None else None
                out["requires"] = export._requires_text(dlg)
                return ok("bounce", "read bounce dialog settings", out)
            finally:
                dialogs.cancel_all(root)

    if not directory:
        return fail("export", "need directory")

    if action == "stems":
        with _front():
            return export.export_stems(root, a.pid, a.name, directory, timeout)

    if not filename:
        return fail("export", "need filename")
    with _front():
        return export.bounce(root, a.pid, a.name, directory, filename, formats or ["wav"],
                             start_bar, end_bar, sample_rate, bit_depth, normalize, mode, include_tail, timeout)


@mcp.tool()
def logic_deliver(
    action: Literal["upload", "bounce_and_upload", "remotes", "list", "setup"],
    directory: str | None = None,
    filename: str | None = None,
    formats: list[str] | None = None,
    paths: list[str] | None = None,
    remote: str | None = None,
    folder: str = "",
    start_bar: int | None = None,
    end_bar: int | None = None,
    make_links: bool = True,
    timeout: float = 1800.0,
) -> dict:
    """Get finished audio out of Logic and into cloud storage.

    bounce_and_upload - the whole chain: render the project to `formats`, verify the files, upload them,
                        and return share links. Give directory + filename + formats + folder.
    upload            - push existing local `paths` to remote:folder.
    remotes / list    - what storage is configured, and what is already in a folder.
    setup             - how the user connects their OWN cloud account.

    Uploads go to the user's own remote: this server ships no credentials and no default account. The
    remote comes from `remote`, else $LOGIC_MCP_REMOTE, else the single configured remote; with several
    configured and nothing pinned it refuses rather than guessing. Run `setup` for the exact commands —
    the user runs them themselves, since the flow ends in a browser sign-in to their own account.
    """
    if action == "setup":
        chosen, err = uploader.resolve_remote(remote)
        return {"remote": chosen, "problem": err, "configured": uploader.remotes(),
                "how_to_connect": uploader.setup_hint()}
    if action == "remotes":
        rs = uploader.remotes()
        chosen, err = uploader.resolve_remote(remote)
        return {"available": uploader.available(), "remotes": rs, "selected": chosen, "problem": err,
                "about": {r: uploader.about(r) for r in rs}}
    if action == "list":
        chosen, err = uploader.resolve_remote(remote)
        if err:
            return fail("rclone", err, uploader.setup_hint())
        return uploader.listing(chosen, folder)
    if action == "upload":
        if not paths:
            return fail("rclone", "need paths")
        return uploader.upload(paths, remote, folder, make_links, timeout)

    if not (directory and filename):
        return fail("deliver", "need directory and filename")
    rendered = logic_export("bounce", directory=directory, filename=filename, formats=formats or ["wav", "mp3"],
                            start_bar=start_bar, end_bar=end_bar, timeout=timeout)
    if rendered.get("state") != "confirmed":
        return rendered | {"stage": "bounce"}
    files = [f["path"] for f in (rendered.get("readback") or {}).get("files", [])]
    up = uploader.upload(files, remote, folder, make_links, timeout)
    return up | {"stage": "upload", "bounced": files}


@mcp.tool()
def logic_plugins(
    action: Literal["insert", "describe", "get", "set", "switch", "preset", "windows", "close"],
    track: int | None = None,
    plugin: str | None = None,
    parameter: str | None = None,
    value: str | float | None = None,
    on: bool = True,
    tolerance: float | None = None,
    enable_band: bool = True,
    window: str | None = None,
) -> dict:
    """Read and write plugin parameters in real units (dB, Hz, %, ratios).

    insert   - add a plugin to a track by name, e.g. plugin="Channel EQ" (uses Mix > Search and Add Plug-in).
    describe - every parameter, switch and menu in an open plugin window, with its current readout.
    get      - one parameter's current value.
    set      - drive a parameter to a target: value="+3 dB", "1.2 kHz", "80 Hz", 2.0. Verified by re-reading
               the plugin's own readout. A parameter whose band is switched off is enabled first when
               enable_band is true, otherwise it refuses rather than silently doing nothing.
    switch   - toggle a named switch (an EQ band, Analyzer, HQ...).
    preset   - read the current preset name.
    windows / close - list or close open plugin editors.
    """
    a = _app()
    if a is None or not a.running:
        return fail("plugins", "Logic is not running")
    if not ax.trusted():
        return fail("plugins", "Accessibility not trusted", "System Settings > Privacy & Security > Accessibility")
    root = ax.app(a.pid)

    if action == "windows":
        return {"windows": [str(ax.title(x) or "") for x in plugs.plugin_windows(root)]}
    if action == "close":
        return ok("plugins", f"closed {plugs.close_plugin_windows(root)}")

    if action == "insert":
        if not plugin:
            return fail("plugins", "need plugin name")
        if track is not None:
            sel = logic_tracks("select", index=track)
            if sel.get("state") == "failed":
                return sel
        with _front():
            return plugs.insert(root, a.pid, a.name, plugin)

    wins = plugs.plugin_windows(root)
    if window:
        wins = [x for x in wins if str(ax.title(x) or "") == window]
    if not wins:
        return fail("plugins", "no plugin editor window is open", "logic_plugins(action='insert', ...) or open one in Logic")
    win = wins[0]

    if action == "describe":
        return plugs.describe(win)
    if action == "preset":
        d = plugs.describe(win)
        return {"window": d["window"], "preset": d["preset"]}

    if not parameter:
        return fail("plugins", "need parameter")

    if action == "switch":
        el, why = plugs.resolve_control(win, parameter, role="AXCheckBox")
        if el is None:
            return fail("plugins", f"{parameter!r} is {why.replace('_', ' ')}",
                        {"switches": [s["name"] for s in plugs.describe(win)["switches"]]})
        r = ax_logic.set_checkbox(el, on)
        return ok("plugins", f"{parameter} = {r['after']}", r) if r["ok"] else unsure("plugins", f"readback {r['after']}", r)

    el, why = plugs.resolve_control(win, parameter, role="AXSlider")
    if el is None:
        hint = ("more than one control matches that name — use the full name"
                if why == "ambiguous" else f"no parameter named {parameter!r}")
        return fail("plugins", hint, {"parameters": [p["name"] for p in plugs.describe(win)["parameters"]]})

    if action == "get":
        real, raw, disp = plugs._read(el)
        return {"window": str(ax.title(win) or ""), "parameter": parameter, "display": disp, "value": real, "raw": raw,
                "enabled": ax.attr(el, "AXEnabled")}

    if value is None:
        return fail("plugins", "need value")
    try:
        tgt, unit = plugs.parse_target(value)
    except ValueError as e:
        return fail("plugins", str(e))
    # a target given in the wrong unit would silently drive the control somewhere absurd
    cur_disp = ax.attr(el, "AXValueDescription")
    cur = plugs.parse_value(cur_disp)
    if unit and cur and cur[1] and unit.lower() != cur[1].lower():
        return fail("plugins", f"{parameter} reads in {cur[1]!r}, but the target was given in {unit!r}",
                    {"current": str(cur_disp)})

    enabled_band = None
    if ax.attr(el, "AXEnabled") is False:
        sw = plugs.owning_switch(win, parameter)
        if sw is None or not enable_band:
            return fail("plugins", f"{parameter} is disabled — its band is switched off",
                        "pass enable_band=true, or switch the band on first")
        enabled_band = ax_logic.set_checkbox(sw, True)
        time.sleep(0.4)

    with _front():
        r = plugs.set_parameter(el, tgt, tolerance)
    r["parameter"] = parameter
    if enabled_band:
        r["enabled_band"] = enabled_band
        if not r.get("ok"):                       # do not leave a band switched on for a write that failed
            sw = plugs.owning_switch(win, parameter)
            if sw is not None:
                r["band_rolled_back"] = ax_logic.set_checkbox(sw, False)["ok"]
    if r.get("ok"):
        return ok("plugins", f"{parameter} {r['start']['display']} -> {r['display']}", r)
    return unsure("plugins", r.get("reason") or f"landed at {r.get('display')}", r)


@mcp.tool()
def logic_level(
    action: Literal["analyse", "plan", "apply", "export_and_plan"],
    paths: list[str] | None = None,
    directory: str | None = None,
    reference: str | None = None,
    apply_pan: bool = True,
    mode: Literal["relative", "absolute"] = "relative",
    max_move_db: float = 9.0,
    headroom_db: float = -1.0,
    max_seconds: float = 600.0,
    dry_run: bool = False,
) -> dict:
    """Measure rendered audio and balance the mix from it.

    Logic's meters report clipping only, with no numbers, so levels are measured from audio rather than read
    off the UI — which is what a human engineer does anyway.

    analyse         - loudness (gated LUFS per BS.1770), true peak, crest, stereo correlation and spectral
                      balance for each file in `paths`.
    plan            - the above, plus a proposed gain and pan per stem. Nothing is applied.
    export_and_plan - render one stem per track out of Logic first, then plan.
    apply           - carry the plan out on the matching tracks, verified in dB. dry_run reports what it
                      would do without touching anything.

    mode matters. "relative" adds each gain to the track's CURRENT fader, which is right when the stems were
    rendered through those faders (export_and_plan). It is NOT idempotent — running it twice applies the move
    twice. "absolute" treats each gain as the fader position itself, which is right for stems that came from
    outside this project, and is safe to re-run.
    """
    if action in ("analyse", "plan", "apply") and not paths and not directory:
        return fail("level", "need paths (or directory) of rendered audio")

    files = list(paths or [])
    if directory and not files:
        import glob, os
        files = sorted(glob.glob(os.path.join(os.path.expanduser(directory), "*.wav")))

    if action == "export_and_plan":
        if not directory:
            return fail("level", "need directory to export stems into")
        a = _app()
        if a is None or not a.running:
            return fail("level", "Logic is not running")
        with _front():
            ex = export.export_stems(ax.app(a.pid), a.pid, a.name, directory)
        if ex.get("state") != "confirmed":
            return ex | {"stage": "export"}
        files = [f["path"] for f in (ex.get("readback") or {}).get("files", [])]
        action = "plan"

    if not files:
        return fail("level", "no audio files found")

    stems = leveling.analyse(files, max_seconds)
    if action == "analyse":
        return {"stems": stems}

    p = leveling.plan(stems, reference, max_move_db, headroom_db)
    if action == "plan" or "error" in p:
        return p | {"summary": leveling.summarise(p)}

    # ---- apply
    w = _win()
    if w is None:
        return fail("ax", *_why_no_win())
    tracks = ax_logic.list_tracks(w)
    applied, unmatched = [], []
    claimed: dict[int, str] = {}                # one track may only be driven by one stem per call
    for m in p["moves"]:
        exact = [t for t in tracks if t["name"].strip().lower() == m["name"].strip().lower()]
        hits = exact or [t for t in tracks if m["name"].lower() in t["name"].lower()]
        if len(hits) != 1:
            unmatched.append({"stem": m["name"], "reason": "no single matching track",
                              "candidates": [t["name"] for t in hits]})
            continue
        t = hits[0]
        if t["index"] in claimed:
            unmatched.append({"stem": m["name"], "reason": f"track {t['name']!r} already taken by stem {claimed[t['index']]!r}"})
            continue
        cur_db = ax_logic.parse_db(t["volume_text"])
        if cur_db is None:
            unmatched.append({"stem": m["name"], "reason": "track fader has no dB readout"})
            continue
        claimed[t["index"]] = m["name"]
        want_db = round((cur_db + m["gain_db"]) if mode == "relative" else m["gain_db"], 1)
        entry = {"stem": m["name"], "track": t["name"], "index": t["index"], "mode": mode,
                 "from_db": cur_db, "gain_db": m["gain_db"], "to_db": want_db,
                 "pan": m["pan"], "pan_action": m.get("pan_action", "set")}
        if dry_run:
            applied.append(entry | {"dry_run": True})
            continue
        r = logic_tracks("set_volume", index=t["index"], db=want_db)
        entry["volume_result"] = r.get("state")
        entry["volume_detail"] = r.get("detail") or r.get("reason")
        if apply_pan and m["pan"] is not None and m.get("pan_action") != "leave":
            pr = logic_tracks("set_pan", index=t["index"], value=max(0, min(127, 64 + round(m["pan"] * 63 / 64))))
            entry["pan_result"] = pr.get("state")
            entry["pan_detail"] = pr.get("detail") or pr.get("reason")
        applied.append(entry)

    detail = {"reference": p["reference"], "mode": mode, "applied": applied, "unmatched": unmatched,
              "summary": leveling.summarise(p)}
    if dry_run:
        return ok("level", f"would apply {len(applied)} move(s)" + (f", {len(unmatched)} unmatched" if unmatched else ""), detail)
    bad_vol = [a for a in applied if a.get("volume_result") != "confirmed"]
    bad_pan = [a for a in applied if a.get("pan_result") not in (None, "confirmed")]
    if unmatched or bad_vol or bad_pan:
        parts = []
        if bad_vol: parts.append(f"{len(bad_vol)} fader(s) unconfirmed")
        if bad_pan: parts.append(f"{len(bad_pan)} pan(s) unconfirmed")
        if unmatched: parts.append(f"{len(unmatched)} stem(s) unmatched")
        return unsure("level", f"{len(applied) - len(bad_vol)} of {len(p['moves'])} fully applied — " + ", ".join(parts), detail)
    return ok("level", f"applied {len(applied)} move(s)", detail)


# --------------------------------------------------------------------------- resources
@mcp.resource("logic://health")
def res_health() -> str:
    return json.dumps(doctor.run(probe_mcu=False))


@mcp.resource("logic://transport")
def res_transport() -> str:
    return json.dumps(transport_state())


@mcp.resource("logic://tracks")
def res_tracks() -> str:
    w = _win()
    return json.dumps({"tracks": ax_logic.list_tracks(w)} if w is not None else {"error": "Accessibility not trusted or no window"})


@mcp.resource("logic://app")
def res_app() -> str:
    a = _app()
    return json.dumps(a.to_dict() if a else None)


@mcp.resource("logic://markers")
def res_markers() -> str:
    """The project's marker list. Marker text lives in AXDescription, not AXValue — reading values gives
    a row per marker with an empty name."""
    pid = _pid()
    root = ax.app(pid) if pid and ax.trusted() else None
    if root is None:
        return json.dumps({"error": "Accessibility not trusted or Logic not running"})
    try:
        found = markers.read_markers(root)
    except Exception as e:                            # the marker window may simply not be open
        return json.dumps({"error": str(e), "markers": None})
    return json.dumps({"markers": found, "count": len(found)})


@mcp.resource("logic://mixer")
def res_mixer() -> str:
    """Mixer state for the 8 strips the OSC surface is showing, plus the selected channel strip.

    Reports only what Logic echoed back. It is the surface's CURRENT bank, never a dead-reckoned one:
    assuming the bank once silently set three wrong tracks to -6.2 dB on a real mix.

    Strictly read-only, and that is a deliberate constraint rather than a convenience. Reading a resource
    must not change anything, so this NEVER binds the surface (which would raise Logic's "OSC app wants to
    connect" alert and click it) and never switches the surface page (which is a visible change and drops
    feedback for the page being left). It reports the already-connected device or explains how to connect.
    """
    dev = _osc_dev
    if dev is None or not dev.connected:
        return json.dumps({"error": "OSC surface not connected",
                           "fix": "logic_osc(action='connect') — reading a resource will not bind it for you"})
    snap = dev.snapshot()
    strip = dev.strip_view() if dev.state.get("/3/trackname") else None
    return json.dumps({
        "bank": snap["bank"],
        "master": snap["master"],
        "selected_strip": strip,
        "selected_strip_note": None if strip else "page 3 has not been visited; logic_osc(action='strip') fetches it",
        "feedback_age_seconds": snap["age"],
        "note": "bank shows the 8 strips currently on the surface, read from Logic's own feedback",
    })


@mcp.resource("logic://regions")
def res_regions() -> str:
    """Regions on the arrange canvas. Offscreen regions are listed with visible=false and no geometry
    rather than omitted, so 'none here' and 'cannot see from here' stay distinguishable."""
    w = _win()
    if w is None:
        return json.dumps({"error": "Accessibility not trusted or no window"})
    regions = ax_logic.list_regions(w)
    return json.dumps({
        "regions": regions,
        "count": len(regions),
        "visible": sum(1 for r in regions if r["visible"]),
        "note": "screen geometry only; bar positions need the ruler's zoom and scroll and are not inferred",
    })


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()


# --------------------------------------------------------------------------- OSC control surface
_osc_dev: osc.LogicPad | None = None
_osc_lock = __import__("threading").Lock()


def _osc_strip_for(index: int | None) -> int | None:
    """Which surface strip (1..8) is showing track `index`, or None if it is not provably on the bank.

    Read from Logic's own feedback, never dead-reckoned. The MCU path once computed the bank as
    divmod(index-1, 8) while the bank buttons were silently doing nothing, and quietly set three
    unrelated tracks to -6.2 dB on a real mix. Same rule here: no proof, no write.
    """
    if index is None:
        return None
    dev = _osc_dev
    if dev is None or not dev.connected:
        return None
    for s in dev.snapshot()["bank"]:
        if s.get("track_no") is not None and int(s["track_no"]) == int(index):
            return s["n"]
    return None


_STALE_IP_FIX = ("the network address changed under a bound device (measured: a Wi-Fi lease change). "
                 "Logic only accepts OSC from the address it resolved, and withdrawing the advert while "
                 "it is bound segfaults Logic, so: quit Logic -> `pkill -f 'dns-sd -P LogicPad'` -> "
                 "restart logicpadd (or this server) -> relaunch Logic; it re-binds on the new address")


def _osc_ready(timeout: float = 10.0) -> tuple[osc.LogicPad | None, dict | None]:
    """The fake LogicPad, connected; (None, failure) when Logic will not bind it."""
    global _osc_dev
    with _osc_lock:                                  # two concurrent calls must not each bind port 9000
        if _osc_dev is not None and _osc_dev.connected and _osc_dev.advertised:
            return _osc_dev, None
    pid = _pid()
    if not pid:
        return None, fail("osc", "Logic is not running")
    if _osc_dev is None:
        # Prefer a running `logicpadd`: only one process on this Mac can hold UDP 9000 plus the Bonjour
        # advert, and the gesture daemon needs the same surface. With no daemon listening `acquire`
        # constructs a LogicPad directly, so this is exactly the old behaviour when nothing else runs.
        try:
            from .gesture.padd import acquire as _acquire
            _osc_dev, _osc_how = _acquire(prefer_daemon=True)
        except OSError as e:
            return None, fail("osc", str(e), fix="join a network (Wi-Fi/Ethernet); Logic refuses loopback OSC devices")
        except Exception:
            try:
                _osc_dev, _osc_how = osc.LogicPad(), "direct"
            except OSError as e:
                return None, fail("osc", str(e), fix="join a network (Wi-Fi/Ethernet); Logic refuses loopback OSC devices")
    # A daemon-backed pad is already bound; it owns the handshake and we must not run a second one.
    if not hasattr(_osc_dev, "connect"):
        if not _osc_dev.connected:
            st = _osc_dev.status()
            if st.get("ip_stale"):
                return None, fail("osc", f"this Mac no longer has {st.get('ip')}, the address logicpadd "
                                         "advertised and Logic bound; every OSC send is being dropped",
                                  fix=_STALE_IP_FIX)
            return None, fail("osc", "logicpadd is running but Logic has not bound its surface",
                              fix="in Logic: Control Surfaces > Setup, or answer the 'OSC app TouchOSC "
                                  "wants to connect' alert; then retry")
        return _osc_dev, None
    if _osc_dev.ip_stale:
        return None, fail("osc", f"this Mac no longer has {_osc_dev.ip}, the address this server "
                                 "advertised and Logic bound; every OSC send is being dropped",
                          fix=_STALE_IP_FIX)
    root = ax.app(pid) if ax.trusted() else None
    presser = (lambda: ax_logic.press_osc_connect(root)) if root is not None else None
    r = _osc_dev.connect(press_alert=presser, timeout=timeout)
    if not r["connected"]:
        return None, fail("osc", "Logic never sent feedback", fix="Logic shows 'OSC app TouchOSC wants to connect' -> click Connect; "
                          "or Logic Pro > Control Surfaces > Setup: the LogicPad device must exist", **r)
    if not any(s["name"] for s in _osc_dev.snapshot()["bank"]):
        _osc_dev.refresh()                       # re-bound to an existing device: Logic did not push state
    if _osc_dev.state.get("/3/trackname") is None and hasattr(_osc_dev, "full_refresh"):
        _osc_dev.full_refresh()                  # Logic de-duplicates feedback; make it resend the strip pages
    return _osc_dev, None


@mcp.tool()
def logic_osc(
    action: Literal["connect", "status", "transport", "select", "mute", "solo", "volume", "pan",
                    "master_volume", "bank", "send", "feedback", "disconnect",
                    "strip", "eq", "eq_set", "plugin", "param", "param_set"],
    command: str | None = None,
    strip: int | None = None,
    value: float | None = None,
    address: str | None = None,
    args: list | None = None,
    wait: float = 1.5,
    band: int | None = None,
    param: str | int | None = None,
    target: float | None = None,
    slot: int | None = None,
) -> dict:
    """Drive Logic through its built-in TouchOSC "LogicPad" control surface over OSC (UDP), with feedback.

    We advertise a Bonjour service in the TouchOSC Mk1 format, Logic binds it as a control surface (one-time
    alert "OSC app TouchOSC wants to connect" -> Connect, pressed automatically), then Logic streams its state
    (track names of the current 8-strip bank, select/mute/solo, volumes, meters, bar, SMPTE time, transport)
    and accepts the fixed LogicPad address map. Faster and more reliable than Mackie emulation for transport,
    selection and mixer moves; every result is read back from Logic's own OSC echo.

    connect        bind the device (idempotent).            status    snapshot of everything Logic last sent.
    transport      command=play|stop|record|cycle|rewind|forward|click|soloreset (record starts recording on
                   armed tracks -- only on explicit request).
    bank           command=bank+|bank-|track+|track-  (move the 8-strip window; strips are 1..8 inside it).
    select|mute|solo  strip=1..8 (mute/solo toggle; readback says the new state).
    volume         strip=1..8 value=0..1 (fader, 0.75 ~ 0 dB).   master_volume value=0..1.
    send           address=/1/... args=[...] raw message; feedback returns what Logic sent in the last `wait` s.

    Selected-track pages (select a track first):
    strip          inserts (slot, name, bypassed), focused plug-in name + its 16 parameters (name, 0..1, display),
                   sends, pan, mute/solo/arm of the SELECTED track.
    eq             the Channel EQ on the selected track: 8 bands (1 high-pass, 2 low shelf, 3-6 peaks, 7 high shelf,
                   8 low-pass) with on/gain/freq/q (+slope on 1 and 8), each as 0..1 and Logic's display value.
                   present=false means the track has no Channel EQ (insert one via logic_plugins first).
    eq_set         band=1..8 param=gain|freq|q|slope|on, then EITHER value=0..1 (raw) OR target=<number in
                   Logic's units: dB, Hz, Q> which is reached by bisection on Logic's display value
                   ("cut the mud": band=4 param=freq target=300; param=gain target=-3; param=q target=1).
    plugin         command=slot+|slot-|page+|page-|bypass|open  (bypass/open take slot=1..12) on the selected track.
    param          list the focused plug-in's parameters (same as strip.params).
    param_set      param=<n or exact name> + value=0..1 or target=<number in the display unit>; read back.
    """
    if action == "disconnect":
        global _osc_dev
        if _osc_dev is not None:
            _osc_dev.close(); _osc_dev = None
        return ok("osc", "device closed")
    dev, err = _osc_ready()
    if err:
        return err
    if action in ("connect", "status", "transport", "bank", "select", "mute", "solo", "volume", "master_volume"):
        dev.ensure_page(1)                           # Logic drops /1/* while the surface sits on another page
    t0 = time.time()
    if action in ("connect", "status"):
        return ok("osc", "connected" if action == "connect" else "snapshot", dev.snapshot())
    if action == "feedback":
        time.sleep(wait)
        return ok("osc", f"{dev.rx_count} messages total", [(round(t - t0, 2), a, v) for t, a, v in dev.recent(since=t0)])
    if action == "send":
        if not address:
            return fail("osc", "address required")
        dev.send(address, *(args or []))
        time.sleep(wait)
        return unsure("osc", "raw message sent; interpret the echo yourself", dev.recent(since=t0, limit=50))
    if action == "transport":
        path = osc.TRANSPORT.get(command or "")
        if not path:
            return fail("osc", f"unknown transport command {command!r}", fix=f"one of {sorted(osc.TRANSPORT)}")
        dev.press(path)
        echo = dev.wait_for(path, timeout=wait, since=t0)
        win = _win()
        snap = ax_logic.transport_snapshot(win) if win is not None else {}
        if command in ("play", "stop") and snap:
            want = command == "play"
            if snap.get("play") is want:
                return ok("osc", f"{command}: Logic transport reads play={snap.get('play')}", {"echo": echo, "transport": snap})
            return unsure("osc", f"{command} sent but Logic transport reads play={snap.get('play')}", {"echo": echo, "transport": snap})
        return (ok if echo is not None else unsure)("osc", f"{command}: echo={echo}", {"echo": echo, "transport": snap})
    if action == "bank":
        path = osc.TRANSPORT.get(command or "")
        if command not in ("bank+", "bank-", "track+", "track-"):
            return fail("osc", "command must be bank+|bank-|track+|track-")
        # Compare track NUMBERS: names repeat across a 137-track project and are None until Logic has
        # sent them, so a name-only comparison reports a real bank move as "uncertain".
        fp = lambda b: [(s["track_no"], s["name"]) for s in b]
        before_bank = dev.snapshot()["bank"]; before = fp(before_bank)
        dev.press(path); time.sleep(wait)
        after_bank = dev.snapshot()["bank"]; after = fp(after_bank)
        moved = after != before
        names = lambda b: [s["name"] for s in b]
        return (ok if moved else unsure)("osc", f"{command}: bank {names(before_bank)} -> {names(after_bank)}", after_bank)
    if action == "pan":
        # Pan lives on page 2, and Logic drops messages for a page the surface is not showing.
        # Worth the page hop: the track-header pan slider only moves by Increment/Decrement and settles
        # within ~4 of 127, so the AX path can never honestly report `confirmed`. Here the echo is
        # Logic's own value, and /2/panval<strip> is its own printed readout ("L23", "C", "R40").
        if not strip or not 1 <= strip <= 8:
            return fail("osc", "strip must be 1..8 (position inside the current bank; use bank to move)")
        if value is None:
            return fail("osc", "value 0..1 required (0 = hard left, 0.5 = centre, 1 = hard right)")
        dev.ensure_page(2)
        path = f"/2/pan{strip}"
        t0 = time.time()
        dev.send(path, float(value))
        echo = dev.wait_for(path, timeout=wait, since=t0)
        name = (dev.state.get(f"/1/trackname{strip}") or [None])[0]
        shown = (dev.state.get(f"/2/panval{strip}") or [None])[0]
        if echo is None:
            return unsure("osc", f"pan strip {strip} ({name}): no echo from Logic",
                          {"strip": strip, "track": name, "asked": value, "shown": shown})
        got = float(echo[0])
        detail = {"strip": strip, "track": name, "asked": value, "pan": got, "shown": shown}
        if abs(got - float(value)) > 0.02:          # same tolerance the volume actions enforce
            return unsure("osc", f"pan strip {strip} ({name}): asked {value}, Logic reads {got}", detail)
        return ok("osc", f"pan strip {strip} ({name}) -> {shown or got}", detail)

    if action in ("select", "mute", "solo", "volume"):
        if not strip or not 1 <= strip <= 8:
            return fail("osc", "strip must be 1..8 (position inside the current bank; use bank to move)")
        if action == "volume":
            if value is None:
                return fail("osc", "value 0..1 required")
            path = f"/1/volume{strip}"
            dev.send(path, float(value))
        else:
            path = f"/1/{action}/1/{strip}"
            dev.press(path)
        echo = dev.wait_for(path, timeout=wait, since=t0)
        name = (dev.state.get(f"/1/trackname{strip}") or [None])[0]
        if echo is None and action == "select" and (dev.state.get(path) or [0.0])[0] == 1.0:
            return ok("osc", f"select strip {strip} ({name}): already selected", dev.snapshot()["bank"][strip - 1])
        if echo is None:
            return unsure("osc", f"{action} strip {strip} ({name}): no echo from Logic", dev.snapshot()["bank"][strip - 1])
        if action == "volume" and abs(float(echo[0]) - float(value)) > 0.02:
            return unsure("osc", f"volume strip {strip} ({name}): asked {value}, Logic reads {echo[0]}", dev.snapshot()["bank"][strip - 1])
        return ok("osc", f"{action} strip {strip} ({name}) -> {echo[0]}", dev.snapshot()["bank"][strip - 1])
    if action == "master_volume":
        if value is None:
            return fail("osc", "value 0..1 required")
        dev.send("/1/mastervolume", float(value))
        echo = dev.wait_for("/1/mastervolume", timeout=wait, since=t0)
        if echo is None:
            return unsure("osc", "master volume: no echo from Logic", dev.snapshot()["master"])
        if abs(float(echo[0]) - float(value)) > 0.02:   # same tolerance the strip volume action enforces
            return unsure("osc", f"master volume: asked {value}, Logic reads {echo[0]}", dev.snapshot()["master"])
        return ok("osc", f"master volume -> {echo[0]}", dev.snapshot()["master"])
    if action in ("strip", "param"):
        dev.ensure_page(3)
        v = dev.strip_view()
        if action == "param":
            return ok("osc", f"{v['track']}: {v['plugin'] or 'no plug-in in the focused slot'}, {len(v['params'])} parameters",
                      {"track": v["track"], "plugin": v["plugin"], "page": v["page"], "params": v["params"]})
        return ok("osc", f"{v['track']}: {len(v['inserts'])} inserts, focused {v['plugin'] or '-'}", v)
    if action == "eq":
        v = dev.eq_view()
        return ok("osc", f"{v['track']}: Channel EQ {'present' if v['present'] else 'NOT present'}", v)
    if action == "eq_set":
        if not band or band not in osc.EQ_BANDS or not param:
            return fail("osc", "band=1..8 and param=gain|freq|q|slope|on required")
        try:
            control, display = dev.eq_address(int(band), str(param))
        except (ValueError, KeyError) as e:
            return fail("osc", str(e))
        dev.page(4, settle=0.5)
        if param == "on":
            dev.press(control)
            echo = dev.wait_for(control, timeout=wait, since=t0)
            return (ok if echo is not None else unsure)("osc", f"band {band} on/off -> {echo}", dev.eq_view()["bands"][int(band) - 1])
        if target is not None:
            r = dev.set_target(control, display, float(target))
            band_state = dev.eq_view()["bands"][int(band) - 1]
            return (ok if r.get("ok") else unsure)("osc", f"band {band} {param} -> {r.get('value')} (target {target})", {"result": r, "band": band_state})
        if value is None:
            return fail("osc", "value=0..1 or target=<number> required")
        r = dev.set_norm(control, display, float(value), timeout=wait)
        return (ok if r["echoed"] else unsure)("osc", f"band {band} {param} -> {r['value']}", {"result": r, "band": dev.eq_view()["bands"][int(band) - 1]})
    if action == "plugin":
        cmds = {"slot+": "/2/slot+", "slot-": "/2/slot-", "page+": "/3/page+", "page-": "/3/page-"}
        if command in cmds:
            before = dev.strip_view()
            # Each of these lives on its OWN page, and Logic drops a message addressed to a page the
            # surface is not showing -- pressing /2/slot+ while page 3 is up is silently ignored.
            dev.ensure_page(int(cmds[command][1]))
            dev.press(cmds[command]); time.sleep(0.6)
            after = dev.strip_view()
            changed = (before["plugin"], before["page"]) != (after["plugin"], after["page"])
            return (ok if changed else unsure)("osc", f"{command}: {before['plugin']} p{before['page']} -> {after['plugin']} p{after['page']}", after)
        if command in ("bypass", "open"):
            if not slot or not 1 <= slot <= 12:
                return fail("osc", "slot=1..12 required")
            root = ax.app(_pid()) if ax.trusted() and _pid() else None
            before = {str(ax.title(x) or "") for x in plugs.plugin_windows(root)} if root else set()
            dev.page(3, settle=0.4)
            path = dev.insert_address(slot, command)
            dev.press(path)
            echo = dev.wait_for(path, timeout=wait, since=t0) if command == "bypass" else None
            v = dev.strip_view()
            ins = next((i for i in v["inserts"] if i["slot"] == slot), None)
            if command == "open":
                # mInsertEdit sends no echo, so the only proof is a plug-in window appearing. Logic ignores
                # it on this build; the reliable opener is the AX one in logic_plugins.
                after = {str(ax.title(x) or "") for x in plugs.plugin_windows(root)} if root else set()
                if new := (after - before):
                    return ok("osc", f"opened {', '.join(sorted(new))}", {"windows": sorted(after), "insert": ins})
                return unsure("osc", f"no plug-in window opened for slot {slot}",
                              {"insert": ins, "windows": sorted(after)},
                              fix="use logic_plugins to open an editor; Logic ignores /3/mInsertEdit here")
            return (ok if echo is not None else unsure)("osc", f"{command} slot {slot}: {ins}", v)
        return fail("osc", "command must be slot+|slot-|page+|page-|bypass|open")
    if action == "param_set":
        dev.ensure_page(3)
        v = dev.strip_view()
        if not v["params"]:
            return fail("osc", f"{v['track']}: no plug-in parameters in the focused slot", fix="plugin slot+/slot- to focus an insert")
        if isinstance(param, str) and not param.isdigit():
            # A plug-in can spread its parameters over several pages of 16, so a name missing here may
            # simply be on the next page; and a multiband plug-in names each band separately.
            # Always start from the plug-in's first parameter page: a previous call may have left it
            # paged forward, and searching from there both misses parameters and lets a loose substring
            # match grab an unrelated one (Multipressor's expander "ExpThrs" for "Threshold").
            for _ in range(6):
                if str(v.get("page") or "1") == "1":
                    break
                dev.ensure_page(3); dev.press("/3/page-"); time.sleep(0.6)
                v = dev.strip_view()
            seen_pages, hit, bands = [], None, []
            for _ in range(4):
                bands = osc.resolve_band_params(param, v["params"])
                hit = osc.resolve_param(param, v["params"])
                if bands or hit:
                    break
                seen_pages.append([p["name"] for p in v["params"]])
                dev.ensure_page(3); dev.press("/3/page+"); time.sleep(0.7)
                v = dev.strip_view()
            if bands:
                results = []
                for bp in bands:
                    c, d = dev.param_address(bp["n"])
                    r = dev.set_target(c, d, float(target)) if target is not None else dev.set_norm(c, d, float(value or 0))
                    results.append({"band": bp["name"], **r})
                good = all(r.get("ok") or r.get("echoed") for r in results)
                return (ok if good else unsure)("osc", f"{v['plugin']} {param} -> {len(bands)} bands: "
                                                f"{[r.get('value') for r in results]}", results)
            if hit is None:
                return fail("osc", f"no parameter named {param!r}", fix=f"pages seen: {seen_pages or [[p['name'] for p in v['params']]]}")
            n = hit["n"]
        else:
            n = int(param) if param is not None else 0
        if not 1 <= n <= 16:
            return fail("osc", "param must be 1..16 or a parameter name")
        control, display = dev.param_address(n)
        name = next((p["name"] for p in v["params"] if p["n"] == n), None)
        if target is not None:
            r = dev.set_target(control, display, float(target))
            return (ok if r.get("ok") else unsure)("osc", f"{v['plugin']} {name} -> {r.get('value')} (target {target})", r)
        if value is None:
            return fail("osc", "value=0..1 or target=<number> required")
        r = dev.set_norm(control, display, float(value), timeout=wait)
        return (ok if r["echoed"] else unsure)("osc", f"{v['plugin']} {name} -> {r['value']}", r)
    return fail("osc", f"unknown action {action}")
