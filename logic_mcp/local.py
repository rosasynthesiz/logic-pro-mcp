"""The compact profile: eight typed tools for a small local model (Qwen3.5 4B first; 9B as a comparison).

Why a second surface instead of hiding tools
--------------------------------------------
The full server has 12 dispatchers and about a hundred actions, among them raw key chords, raw MIDI, menu
presses, recording, deletion, cloud upload and binary edits of saved projects. A 4B model given that
surface has every opportunity to pick the wrong one. Hiding names from `tools/list` is not enough: a
client can call any name it likes. So this profile is a SEPARATE server with its own `tools/call` handler
that knows exactly eight names, validates every argument against a hand-written schema (unknown keys
rejected, numbers finite and bounded, booleans never numbers) and refuses everything else before any
channel is touched. There is no generic command/params tool, no approval flag a model could set on
itself, and no action that records, deletes, uploads, quits, sends raw keys/MIDI/OSC or writes a file.

What the eight tools promise
----------------------------
* health / read_transport / list_tracks are passive: they publish no MIDI port, bind no OSC surface,
  click nothing. They report what exists, and null means "not known".
* play / stop / set_tempo / set_mute / set_gain are the only writes. Each one checks that the screen is
  unlocked, Logic shows a project and no alert or sheet is up; resolves its target exactly (index AND
  name must agree with a fresh read); takes the mutation guard; drives the existing channels; and is
  `confirmed` only on fresh readback. set_gain is capped at +6 dB and to a 12 dB move per call.
* Outputs are bounded: paged track lists, readback trimmed to a few keys, strings cut short.

Everything flows through the same `logic_transport` / `logic_tracks` code the full profile uses: this is
a policy layer over the existing channels, not a second implementation. None of it has been exercised
against a running Logic or a running model here; see docs/local-model.md for what was and was not tested.
"""
from __future__ import annotations

import json
import math
from typing import Any, Callable

from . import doctor, policy, server, target
from .channels import ax, ax_logic, dialogs
from .result import fail

GAIN_MIN_DB, GAIN_MAX_DB, GAIN_MAX_STEP_DB = -60.0, 6.0, 12.0
TEMPO_MIN, TEMPO_MAX = 5.0, 990.0
LIST_MAX, LIST_DEFAULT = 50, 20
NAME_MAX = 64
TEXT_MAX = 300
FADER_DOWN_DB = -100.0            # how a fader at -inf is reported: JSON has no infinity

INSTRUCTIONS = (
    "Logic Pro, local profile. Call health first. Read before you write: list_tracks, then copy BOTH "
    "track_index and track_name into set_mute / set_gain. A result is true only when state is \"confirmed\"; "
    "on \"uncertain\" read again and never repeat the write; on \"failed\" tell the human the fix."
)

_tools: dict[str, dict] = {}
_impls: dict[str, Callable[..., dict]] = {}


def tool(name: str, description: str, schema: dict):
    full = {"type": "object", "properties": {}, "additionalProperties": False, **schema}

    def deco(fn):
        _tools[name] = {"name": name, "description": description, "inputSchema": full}
        _impls[name] = fn
        return fn
    return deco


def tools() -> list[dict]:
    """The tools/list payload, in registration order."""
    return [dict(t) for t in _tools.values()]


def call(name: str, arguments: Any) -> dict:
    """Validate and dispatch one call. The ONLY entry point: the MCP handler and the tests both use it."""
    spec = _tools.get(name)
    if spec is None:
        return fail("policy", f"unknown tool {name!r}; this profile serves {sorted(_tools)}", refused=True)
    try:
        clean = policy.validate(arguments, spec["inputSchema"])
    except ValueError as e:
        return fail("policy", f"{name}: {e}", "fix the arguments; nothing was sent to Logic", refused=True)
    return bound(_impls[name](**clean))


# ---------------------------------------------------------------- bounded output
_KEEP = ("state", "channel", "detail", "reason", "fix", "accepted", "refused", "project_changed")
_READBACK_KEYS = ("playing", "recording", "tempo", "index", "name", "mute", "solo", "arm", "selected", "volume_text",
                  "before", "after", "verified", "display", "before_db", "after_db", "how", "strip")


def _short(v: Any) -> Any:
    if isinstance(v, str):
        return v if len(v) <= TEXT_MAX else v[:TEXT_MAX] + "…"
    if isinstance(v, float) and not math.isfinite(v):
        return FADER_DOWN_DB if v < 0 else None
    return v


def bound(res: dict) -> dict:
    """Trim a dispatcher result to what a small model needs: the verdict, the reason, the fix, a little readback."""
    if "state" not in res:                      # a plain read: already shaped by the tool
        return res
    out = {k: _short(res[k]) for k in _KEEP if k in res}
    rb = res.get("readback")
    if isinstance(rb, dict):
        out["readback"] = {k: _short(rb[k]) for k in _READBACK_KEYS if k in rb and not isinstance(rb[k], (dict, list))}
    return out


# ---------------------------------------------------------------- guards every write runs first
def _preflight() -> dict | None:
    """Why a write must not start: locked screen, Logic not running, no project window, an alert or sheet up."""
    if not doctor.session_state()["ok"]:
        return fail("session", "screen locked or display asleep; Logic cannot be seen", "unlock the Mac", refused=True)
    a = target.find()
    if a is None or not a.running:
        return fail("app", "Logic is not running", "open Logic Pro and the project", refused=True)
    if not ax.trusted():
        return fail("ax", "Accessibility not trusted", "System Settings > Privacy & Security > Accessibility", refused=True)
    root = ax.app(a.pid)
    w = ax_logic.main_window(root)
    if w is None:
        return fail("ax", "no project window open in Logic", "open or create a project", refused=True)
    alert = dialogs.alert(root)
    sheet = next((c for c in ax.children(w) if ax.role(c) == "AXSheet"), None)
    if alert is not None or sheet is not None:
        what = str(ax.title(alert) or "alert") if alert is not None else "a sheet on the project window"
        return fail("dialog", f"Logic is showing {what}; nothing was sent", "answer or close it in Logic first", refused=True)
    return None


def _write(label: str, fn: Callable[[], dict]) -> dict:
    try:
        with policy.guard.held(label):
            return fn()
    except policy.Busy as e:
        return fail("policy", f"refusing {label}: {e}", "wait, then read the state before retrying", refused=True)


# ---------------------------------------------------------------- the eight tools
@tool("health", "Passive check, call first: is Logic running, which project is open, which channels work. Changes nothing.",
      {})
def health() -> dict:
    d = doctor.run(probe_mcu=False)
    a = d.get("app") or {}
    w = None
    if a.get("running") and ax.trusted() and doctor.session_state()["ok"]:
        w = ax_logic.main_window(ax.app(a["pid"]))
    title = ax_logic.project_identity(w)["title"] if w is not None else ""
    return {"ok": not d["blocked"] and w is not None,
            "logic": "running" if a.get("running") else ("installed" if a else "not_installed"),
            "project": (title.removesuffix(" - Tracks")[:NAME_MAX] or None) if w is not None else None,
            "ready": d["ready"], "blocked": d["blocked"], "fix": [_short(f) for f in d["fix_plan"][:3]]}


@tool("read_transport", "Passive read: playing, recording, tempo_bpm, bar. null means not known. Changes nothing.", {})
def read_transport() -> dict:
    s = server.transport_state(passive=True)
    pos = s.get("position") or {}
    return {"playing": s.get("playing"), "recording": s.get("recording"), "tempo_bpm": s.get("tempo"),
            "bar": pos.get("bar"), "beat": pos.get("beat"), "sources": s.get("sources") or [],
            "reason": s.get("reason")}


@tool("list_tracks", "Tracks of the open project: index (1 = top), name, muted, soloed, armed, volume_db "
                     f"({FADER_DOWN_DB:.0f} = fader fully down). Paged by offset/limit. Copy index AND name into set_mute / set_gain.",
      {"properties": {"offset": {"type": "integer", "minimum": 0},
                      "limit": {"type": "integer", "minimum": 1, "maximum": LIST_MAX}}})
def list_tracks(offset: int = 0, limit: int = LIST_DEFAULT) -> dict:
    w = server._win()
    if w is None:
        return fail("ax", *server._why_no_win())
    rows = ax_logic.list_tracks(w)
    page = rows[offset:offset + limit]
    return {"total": len(rows), "offset": offset, "limit": limit,
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "tracks": [{"index": t["index"], "name": (t["name"] or "")[:NAME_MAX], "muted": t["mute"],
                        "soloed": t["solo"], "armed": t["arm"],
                        "volume_db": _short(ax_logic.parse_db(t.get("volume_text")))} for t in page]}


@tool("play", "Start playback. confirmed only when Logic reads back playing=true.", {})
def play() -> dict:
    return _preflight() or _write("play", lambda: server.logic_transport("play"))


@tool("stop", "Stop playback. confirmed only when Logic reads back playing=false.", {})
def stop() -> dict:
    return _preflight() or _write("stop", lambda: server.logic_transport("stop"))


@tool("set_tempo", f"Set the project tempo in beats per minute ({TEMPO_MIN:.0f}..{TEMPO_MAX:.0f}). confirmed by reading the tempo back.",
      {"properties": {"bpm": {"type": "number", "minimum": TEMPO_MIN, "maximum": TEMPO_MAX}}, "required": ["bpm"]})
def set_tempo(bpm: float) -> dict:
    return _preflight() or _write("set_tempo", lambda: server.logic_transport("set_tempo", tempo=float(bpm)))


_TARGET = {"track_index": {"type": "integer", "minimum": 1},
           "track_name": {"type": "string", "minLength": 1, "maxLength": NAME_MAX}}


@tool("set_mute", "Mute (true) or unmute (false) one track. track_index and track_name must both come from list_tracks and "
                  "name the same track, or the call is refused.",
      {"properties": {**_TARGET, "muted": {"type": "boolean"}}, "required": ["track_index", "track_name", "muted"]})
def set_mute(track_index: int, track_name: str, muted: bool) -> dict:
    return _preflight() or _write("set_mute", lambda: server.logic_tracks("mute", index=track_index, name=track_name, on=muted))


@tool("set_gain", f"Set one track's fader to gain_db ({GAIN_MIN_DB:.0f}..+{GAIN_MAX_DB:.0f} dB), at most {GAIN_MAX_STEP_DB:.0f} dB away "
                  "from its current value per call. track_index and track_name must both match the same track in list_tracks.",
      {"properties": {**_TARGET, "gain_db": {"type": "number", "minimum": GAIN_MIN_DB, "maximum": GAIN_MAX_DB}},
       "required": ["track_index", "track_name", "gain_db"]})
def set_gain(track_index: int, track_name: str, gain_db: float) -> dict:
    if why := _preflight():
        return why
    w = server._win()
    try:
        item = ax_logic.resolve_track(w, track_index, track_name, fuzzy=False)
    except ax_logic.TargetError as e:
        return fail("ax", f"refusing set_gain: {e}", "call list_tracks and copy index + name", refused=True)
    cur = ax_logic.track_db(item)
    if cur is None:
        return fail("ax", "this track's fader has no dB readout", "set it in Logic", refused=True)
    base = max(cur, GAIN_MIN_DB)                 # a fader at -inf counts as the floor, so it can be brought up in steps
    if abs(float(gain_db) - base) > GAIN_MAX_STEP_DB:
        return fail("policy", f"refusing: {gain_db:+.1f} dB is more than {GAIN_MAX_STEP_DB:.0f} dB from the current "
                              f"{'-inf' if cur == float('-inf') else f'{cur:+.1f}'} dB",
                    f"move in steps of at most {GAIN_MAX_STEP_DB:.0f} dB", refused=True)
    return _write("set_gain", lambda: server.logic_tracks("set_volume", index=track_index, name=track_name, db=float(gain_db)))


# ---------------------------------------------------------------- measurement
def schema_stats(tool_list: list[dict] | None = None) -> dict:
    """Characters (not tokens) a client ships to the model for this tools/list."""
    ts = tool_list if tool_list is not None else tools()
    return {"tools": len(ts),
            "enum_choices": sum(len(p.get("enum", [])) for t in ts for p in t["inputSchema"].get("properties", {}).values()),
            "schema_chars": sum(len(json.dumps(t["inputSchema"], separators=(",", ":"))) for t in ts),
            "description_chars": sum(len(t.get("description") or "") for t in ts)}


# ---------------------------------------------------------------- MCP wiring
def build_server():
    """A low-level MCP server whose tools/list is `tools()` and whose tools/call is `call()`: nothing else exists."""
    import anyio
    from mcp import types
    from mcp.server.lowlevel import Server

    async def on_list_tools(ctx, params):
        return types.ListToolsResult.model_validate({"tools": tools()})

    async def on_call_tool(ctx, params):
        res = await anyio.to_thread.run_sync(call, params.name, params.arguments or {})
        return types.CallToolResult.model_validate({
            "content": [{"type": "text", "text": json.dumps(res, ensure_ascii=False)}],
            "structuredContent": res,
            "isError": bool(res.get("refused")),
        })

    return Server("logic-pro-mcp-local", instructions=INSTRUCTIONS, on_list_tools=on_list_tools, on_call_tool=on_call_tool)


def main() -> None:
    import anyio
    from mcp.server.stdio import stdio_server

    async def _run():
        srv = build_server()
        async with stdio_server() as (r, w):
            await srv.run(r, w, srv.create_initialization_options())
    anyio.run(_run)


if __name__ == "__main__":
    main()
