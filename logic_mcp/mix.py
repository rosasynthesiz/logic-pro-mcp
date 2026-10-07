"""Natural-language mix requests -> Logic stock-plugin settings, applied through the plugin window.

"cut the mud in the strings" resolves to: the STRINGS folder/bus track (or every track in that family),
the channel strip's Channel EQ -- opened from the Inspector's EQ thumbnail, which creates one when the
strip has none -- and a recipe of parameter targets driven with plugins.set_parameter and read back.

The recipe table is deliberately conservative starting points. It is data, not judgement: the
engineer's ears (or a measurement) decide whether -3 dB was enough.
"""
from __future__ import annotations

import re
import time

from . import plugins as plugs
from .channels import ax, ax_logic, dialogs, mouse, screen
from .result import fail, ok, unsure

FAMILY_WORDS = {"strings": "strings", "string": "strings", "vox": "vocals", "vocal": "vocals", "vocals": "vocals",
                "drums": "drums", "drum": "drums", "perc": "percussion", "percussion": "percussion", "bass": "bass",
                "guitar": "guitar", "guitars": "guitar", "gtr": "guitar", "keys": "keys", "piano": "keys",
                "synth": "synth", "brass": "brass", "winds": "woodwind", "wind": "woodwind", "fx": "fx", "mix": "mix", "master": "mix"}
FOLDER_FOR = {"strings": "STRINGS", "vocals": "VOX", "drums": "DRUMS", "percussion": "PERC", "bass": "BASS",
              "guitar": "GUITARS", "keys": "KEYS", "synth": "SYNTH", "woodwind": "WINDS", "fx": "FX"}

import json as _json
import os as _os

# Reviewed recipe table (authored + adversarially reviewed against engineering references); each entry:
# {"terms": [...], "plugin": "...", "settings": [{"parameter","value","unit"}], "by_source": {...}, "caution", "sources"}
with open(_os.path.join(_os.path.dirname(__file__), "mix_recipes.json"), encoding="utf-8") as _f:
    RECIPE_TABLE: list[dict] = _json.load(_f)


def lookup(command: str) -> dict | None:
    """Longest matching term wins, so 'more air' beats 'air' and 'high-pass' beats 'pass'."""
    text = command.lower()
    best = None
    for r in RECIPE_TABLE:
        for t in r["terms"]:
            if t.lower() in text and (best is None or len(t) > len(best[0])):
                best = (t, r)
    return best[1] if best else None


def family_of(command: str) -> str | None:
    text = command.lower()
    return next((v for k, v in FAMILY_WORDS.items() if re.search(rf"\b{k}\b", text)), None)


def resolve_track(win, family: str | None, track_name: str | None) -> dict | None:
    tracks = ax_logic.list_tracks(win)
    if track_name:
        return next((t for t in tracks if t["name"] == track_name), None)
    if family and FOLDER_FOR.get(family):
        return next((t for t in tracks if t["is_folder"] and t["name"] == FOLDER_FOR[family]), None)
    return None


def _inspector_eq_button(win):
    for _, el in ax.walk(win, max_depth=14):
        if ax.role(el) == "AXButton" and str(ax.desc(el) or "") == "EQ":
            r = screen.element_rect(el)
            if r and r[0] < 320:
                return el
    return None


def open_channel_eq(root, win, pid: int, timeout: float = 6.0):
    """Press the Inspector's EQ thumbnail for the selected track: opens Channel EQ, creating it if absent."""
    before = {str(ax.title(x) or "") for x in plugs.plugin_windows(root)}
    # The Inspector redraws asynchronously after a track selection: looking immediately finds no EQ
    # thumbnail and the recipe fails, which is what made a back-to-back run of the recipes report every
    # Channel EQ command as "no EQ thumbnail" while the same command passed when run on its own.
    btn = dialogs.wait_for(lambda: _inspector_eq_button(win), 3.0)
    if btn is None:
        return None, "no EQ thumbnail in the Inspector (is the Inspector shown?)"
    ax.press(btn)
    w = dialogs.wait_for(lambda: next((x for x in plugs.plugin_windows(root) if str(ax.title(x) or "") not in before), None), 2.5)
    if w is None:
        r = screen.element_rect(btn)
        if r:
            mouse.double_click(r[0] + r[2] / 2, r[1] + r[3] / 2)
            w = dialogs.wait_for(lambda: next((x for x in plugs.plugin_windows(root) if str(ax.title(x) or "") not in before), None), timeout)
    return (w, "ok") if w is not None else (None, "Channel EQ window did not open")


def _apply_over_osc(rec: dict, t: dict) -> dict | None:
    """Set a recipe's parameters through the OSC control surface.

    Used when no plug-in editor window is available. It is also the more accurate route: it bisects
    against the value Logic displays, handles Logic's abbreviated names, inverted parameters and
    multiband plug-ins, none of which the AX slider path does.
    """
    try:
        from .server import _osc_ready, logic_osc
    except Exception:
        return None
    dev, err = _osc_ready()
    if dev is None:
        return None
    slot = None
    for i in dev.strip_view()["inserts"]:
        if i["name"] and rec["plugin"].lower().startswith(i["name"].lower().rstrip("*")[:6].lower()):
            slot = i["slot"]
    if slot is not None:
        logic_osc("plugin", command="open", slot=slot)
        time.sleep(1.0)
    steps = {}
    for s_ in rec["settings"]:
        val = float(s_["value"]) * (1000.0 if s_["unit"] == "kHz" else 1.0)
        r = logic_osc("param_set", param=s_["parameter"], target=val)
        steps[s_["parameter"]] = {"ok": r.get("state") == "confirmed", "detail": r.get("detail") or r.get("reason")}
    bad = [k for k, v in steps.items() if not v["ok"]]
    detail = {"track": t["name"], "recipe": rec["terms"][0], "plugin": rec["plugin"], "route": "osc",
              "settings": rec["settings"], "caution": rec["caution"], "steps": steps}
    if bad:
        return unsure("mix", f"{rec['terms'][0]} on {t['name']} via {rec['plugin']} (OSC): unmet {bad}", detail)
    return ok("mix", f"{rec['terms'][0]} on {t['name']} via {rec['plugin']} (OSC): "
              + ", ".join(f"{x['parameter']}={x['value']}{x['unit']}" for x in rec["settings"]), detail)


def apply(root, win, pid: int, command: str, track_name: str | None = None) -> dict:
    rec = lookup(command)
    fam = family_of(command)
    if rec is None:
        return fail("mix", f"no known request in {command!r}", {"known": sorted({t for r in RECIPE_TABLE for t in r["terms"]})})
    t = resolve_track(win, fam, track_name)
    if t is None:
        return fail("mix", f"could not find a track for {track_name or fam!r}")
    # Deliberately NOT retried: a header click that misses can drag the track and reorder the
    # arrangement, so a second attempt doubles that risk rather than recovering from it.
    try:
        if not ax_logic.click_select_track(win, t["index"], pid=pid):
            return fail("mix", f"could not select {t['name']}")
    except ax_logic.TrackMoved as e:
        return fail("mix", str(e), fix="select the track by hand, then pass track_name= to apply()")
    time.sleep(0.5)
    if rec["plugin"] == "Channel EQ":
        w, why = open_channel_eq(root, win, pid)
    else:
        before = {str(ax.title(x) or "") for x in plugs.plugin_windows(root)}
        r = plugs.insert(root, pid, None, rec["plugin"])
        # Logic titles a plug-in window with the TRACK name, never the plug-in name, so matching the
        # plug-in name against the title never hits: every non-EQ recipe failed with "no editor window".
        # Take whichever plug-in window is new, else fall back to the OSC surface below.
        w = next((x for x in plugs.plugin_windows(root) if str(ax.title(x) or "") not in before), None)
        if w is None:
            w = next((x for x in plugs.plugin_windows(root) if str(ax.title(x) or "") == t["name"]), None)
        why = r.get("reason", "") if w is None else "ok"
        # Prefer the control surface for non-EQ plug-ins. Logic's plug-in WINDOWS expose mangled,
        # duplicated Accessibility names ("MIX", "THRESHOLD", "Compressor" twice) that resolve to
        # nothing, so the AX slider route reports every setting not_found; the OSC surface gives clean
        # names and bisects against Logic's own display. AX stays as the fallback.
        if r.get("state") in ("confirmed", "uncertain"):
            osc_res = _apply_over_osc(rec, t)
            if osc_res is not None:
                return osc_res
    if w is None:
        return fail("mix", why)
    steps = {}
    for s_ in rec["settings"]:
        pname = s_["parameter"]
        band = " ".join(pname.split()[:-1]) if pname.split()[-1] in ("Frequency", "Gain", "Q") else None
        if band and f"{band} on" not in steps:
            sw = plugs.find_control(w, band, role="AXCheckBox")
            if sw is not None:
                steps[f"{band} on"] = ax_logic.set_checkbox(sw, True)
        val = float(s_["value"]) * (1000.0 if s_["unit"] == "kHz" else 1.0)
        el, why = plugs.resolve_control(w, pname, role="AXSlider")
        steps[pname] = plugs.set_parameter(el, val) if el is not None else {"ok": False, "reason": why}
    bad = [k for k, v in steps.items() if not (isinstance(v, dict) and v.get("ok"))]
    detail = {"track": t["name"], "recipe": rec["terms"][0], "plugin": rec["plugin"], "settings": rec["settings"],
              "source_note": rec["by_source"].get(fam or "", ""), "caution": rec["caution"], "steps": steps, "window": str(ax.title(w) or "")}
    if bad:
        return unsure("mix", f"applied with problems: {bad}", detail)
    return ok("mix", f"{rec['terms'][0]} on {t['name']} via {rec['plugin']}: " + ", ".join(f"{x['parameter']}={x['value']}{x['unit']}" for x in rec["settings"]), detail)
