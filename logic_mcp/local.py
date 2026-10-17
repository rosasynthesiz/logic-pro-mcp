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
* Outputs are bounded: paged track lists, readback trimmed to a few keys, strings cut short, and every
  `fix` rewritten in this profile's own vocabulary (a hint naming a full-profile tool is useless here and,
  measured, gets parroted to the human).

What the first model run taught (docs/local-model-eval.md)
----------------------------------------------------------
Every out-of-range value, stale index, locked screen and alert was stopped by the server; the failures
that got through were the model acting on a GUESS the surface made easy: a refusal that named the track
actually at that index was read as "mute that one instead"; "12 dB per call" was read as an amount to
add; `limit=100` was refused instead of clamped; `offset` was read as 1-based; duplicate names were
resolved by picking the first; solo was emulated with mutes, flip-flopping. Hence: refusals say what is
wrong without suggesting another target, list_tracks flags duplicate names and clamps the page size,
set_gain says it is absolute and tells the farthest reachable value, and a reversal of the same track's
mute or fader direction within a few seconds is refused as thrash.

Everything flows through the same `logic_transport` / `logic_tracks` code the full profile uses: this is
a policy layer over the existing channels, not a second implementation. None of it has been exercised
against a running Logic; the model half of the acceptance gate is run by scripts/local_eval.py.
"""
from __future__ import annotations

import json
import math
import time
from collections import Counter
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
THRASH_WINDOW_S = 10.0            # a reversal of the same control inside this window is refused

INSTRUCTIONS = (
    "Logic Pro, local profile: these eight tools and nothing else. "
    "1) health first; read before you write: list_tracks before set_mute/set_gain, read_transport before set_tempo. "
    "2) Copy track_index AND track_name from the SAME row of list_tracks. If the human's index and name point at "
    "different rows, or the name has duplicate_name=true and the human gave no index, stop and ask; never pick one. "
    "3) Never invent a number: no tempo given, a percent, 'a bit', 'louder' -> ask for the value in bpm or dB. "
    "A relative change ('up 10 bpm', '3 dB quieter') -> read first, compute, say both numbers, then set once. "
    "4) Not available here: record, arm, solo, pan, delete, rename, plug-ins, bounce, upload. Say so and stop; "
    "do not emulate (solo is not 'mute the others'; play is not record). "
    "5) A value outside a limit is refused: report the refusal, do not substitute a nearer value. "
    "6) Only state \"confirmed\" is true. On \"uncertain\" read again and tell the human, never repeat the write. "
    "On \"failed\" report the fix. Say only what the readback shows."
)

_tools: dict[str, dict] = {}
_impls: dict[str, Callable[..., dict]] = {}
_recent: dict[tuple, tuple[float, float, float]] = {}      # (kind, index, name) -> (from_value, to_value, when)
_now = time.monotonic


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
_KEEP = ("state", "channel", "detail", "reason", "fix", "accepted", "refused", "mismatch", "project_changed")
_READBACK_KEYS = ("playing", "recording", "tempo", "index", "name", "mute", "solo", "arm", "selected", "volume_text",
                  "before", "after", "verified", "display", "before_db", "after_db", "how", "strip")
_UNCERTAIN_FIX = "read list_tracks or read_transport; if Logic still shows the old state tell the human; do not repeat the write"
_FAILED_FIX = "tell the human what Logic reports; call list_tracks or read_transport if you need the current state"


def _short(v: Any) -> Any:
    if isinstance(v, str):
        return v if len(v) <= TEXT_MAX else v[:TEXT_MAX] + "…"
    if isinstance(v, float) and not math.isfinite(v):
        return FADER_DOWN_DB if v < 0 else None
    return v


def bound(res: dict) -> dict:
    """Trim a dispatcher result to what a small model needs: the verdict, the reason, a fix in THIS profile's
    vocabulary, a little readback. A full-profile hint ("logic_osc(action='mute' ...)") is replaced: measured,
    the model repeats it to the human as if it were advice."""
    if "state" not in res:                      # a plain read: already shaped by the tool
        return res
    out = {k: _short(res[k]) for k in _KEEP if k in res}
    fix = res.get("fix")
    if res.get("state") == "uncertain":
        out["fix"] = _UNCERTAIN_FIX
    elif res.get("state") == "failed" and (not fix or "logic_" in str(fix)):
        out["fix"] = _FAILED_FIX
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


def _target(w, index: int, name: str) -> tuple[Any, dict | None]:
    """The one header both selectors describe, or a refusal that names NO other track.

    The first run showed why the wording matters: told "track 1 is named 'Intro', not 'Vocal'", the 4B model
    muted Intro. The refusal now says only that the pair does not match and what to do."""
    try:
        return ax_logic.resolve_track(w, index, name, fuzzy=False), None
    except ax_logic.TargetError as e:
        msg = str(e)
        if msg.startswith("no track at index"):
            return None, fail("ax", f"{msg}: the list you used is stale", "call list_tracks again", refused=True)
        return None, fail("ax", f"track_index {index} and track_name {name!r} are not the same track; the list you "
                                "used is stale or the human's request is contradictory",
                          "call list_tracks; if index and name still disagree, ask the human which track. Do not pick one",
                          refused=True, mismatch=True)


def _thrash(kind: str, index: int, name: str, from_v: float, to_v: float) -> dict | None:
    """Refuse a reversal of the same control inside THRASH_WINDOW_S: measured, a model asked for solo emulated it by
    flipping mute back and forth six times. A human who changes their mind repeats the request after the window."""
    last = _recent.get((kind, index, name))
    if last is None:
        return None
    l_from, l_to, when = last
    age = _now() - when
    if age > THRASH_WINDOW_S:
        return None
    if kind == "mute":
        reversed_ = to_v != l_to
    else:
        reversed_ = (to_v - l_to) * (l_to - l_from) < 0 and abs(to_v - l_to) > 1.0
    if not reversed_:
        return None
    return fail("policy", f"refusing: this track's {kind} was set the other way {age:.0f} s ago by this session; "
                          "a reversal needs the human to ask for it",
                "tell the human what list_tracks shows and wait for their answer", refused=True)


def _write(label: str, fn: Callable[[], dict]) -> dict:
    try:
        with policy.guard.held(label):
            return fn()
    except policy.Busy as e:
        return fail("policy", f"refusing {label}: {e}", "wait, then read the state before retrying", refused=True)


def _remember(kind: str, index: int, name: str, from_v: float, to_v: float, res: dict) -> dict:
    if res.get("state") != "failed":
        _recent[(kind, index, name)] = (from_v, to_v, _now())
    return res


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


@tool("list_tracks", "Tracks of the open project, top to bottom: index (1 = top), name, muted, soloed, armed, volume_db "
                     f"({FADER_DOWN_DB:.0f} = fader fully down), duplicate_name (true = another track has the same name: ask the "
                     "human which index before writing). offset = tracks to skip (0 = start at track 1); limit = page size, "
                     f"at most {LIST_MAX} (larger is clamped). Copy index AND name from one row into set_mute / set_gain.",
      {"properties": {"offset": {"type": "integer", "minimum": 0},
                      "limit": {"type": "integer", "minimum": 1}}})
def list_tracks(offset: int = 0, limit: int = LIST_DEFAULT) -> dict:
    limit = min(limit, LIST_MAX)                 # a read's page size is clamped, not refused: measured, the refusal only cost a turn
    w = server._win()
    if w is None:
        return fail("ax", *server._why_no_win())
    rows = ax_logic.list_tracks(w)
    dup = Counter((t["name"] or "").strip().lower() for t in rows)
    page = rows[offset:offset + limit]
    return {"total": len(rows), "offset": offset, "limit": limit,
            "from_index": page[0]["index"] if page else None, "to_index": page[-1]["index"] if page else None,
            "next_offset": offset + limit if offset + limit < len(rows) else None,
            "tracks": [{"index": t["index"], "name": (t["name"] or "")[:NAME_MAX], "muted": t["mute"],
                        "soloed": t["solo"], "armed": t["arm"],
                        "volume_db": _short(ax_logic.parse_db(t.get("volume_text"))),
                        "duplicate_name": dup[(t["name"] or "").strip().lower()] > 1} for t in page]}


@tool("play", "Start playback. Not recording: there is no record tool here. confirmed only when Logic reads back playing=true.", {})
def play() -> dict:
    return _preflight() or _write("play", lambda: server.logic_transport("play"))


@tool("stop", "Stop playback. confirmed only when Logic reads back playing=false.", {})
def stop() -> dict:
    return _preflight() or _write("stop", lambda: server.logic_transport("stop"))


@tool("set_tempo", f"Set the project tempo to an absolute value in beats per minute ({TEMPO_MIN:.0f}..{TEMPO_MAX:.0f}). For a "
                   "relative change call read_transport first and compute. confirmed by reading the tempo back.",
      {"properties": {"bpm": {"type": "number", "minimum": TEMPO_MIN, "maximum": TEMPO_MAX}}, "required": ["bpm"]})
def set_tempo(bpm: float) -> dict:
    return _preflight() or _write("set_tempo", lambda: server.logic_transport("set_tempo", tempo=float(bpm)))


_TARGET = {"track_index": {"type": "integer", "minimum": 1},
           "track_name": {"type": "string", "minLength": 1, "maxLength": NAME_MAX}}


@tool("set_mute", "Mute (true) or unmute (false) one track. track_index and track_name must come from the same row of "
                  "list_tracks; a mismatch is refused. Not solo, not arm.",
      {"properties": {**_TARGET, "muted": {"type": "boolean"}}, "required": ["track_index", "track_name", "muted"]})
def set_mute(track_index: int, track_name: str, muted: bool) -> dict:
    if why := _preflight():
        return why
    item, refusal = _target(server._win(), track_index, track_name)
    if refusal:
        return refusal
    cur = ax_logic.track_info(item).get("mute")
    if why := _thrash("mute", track_index, track_name, float(bool(cur)), float(muted)):
        return why
    res = _write("set_mute", lambda: server.logic_tracks("mute", index=track_index, name=track_name, on=muted))
    return _remember("mute", track_index, track_name, float(bool(cur)), float(muted), res)


@tool("set_gain", f"Set one track's fader to an ABSOLUTE position gain_db in dB ({GAIN_MIN_DB:.0f}..+{GAIN_MAX_DB:.0f}); it is not an "
                  f"amount to add. One call may move the fader at most {GAIN_MAX_STEP_DB:.0f} dB from where it is now; a refusal "
                  "tells you the farthest reachable value. Percent or words are not dB: ask. track_index and track_name must "
                  "come from the same row of list_tracks.",
      {"properties": {**_TARGET, "gain_db": {"type": "number", "minimum": GAIN_MIN_DB, "maximum": GAIN_MAX_DB}},
       "required": ["track_index", "track_name", "gain_db"]})
def set_gain(track_index: int, track_name: str, gain_db: float) -> dict:
    if why := _preflight():
        return why
    item, refusal = _target(server._win(), track_index, track_name)
    if refusal:
        return refusal
    cur = ax_logic.track_db(item)
    if cur is None:
        return fail("ax", "this track's fader has no dB readout", "set it in Logic", refused=True)
    target_db = float(gain_db)
    base = max(cur, GAIN_MIN_DB)                 # a fader at -inf counts as the floor, so it can be brought up in steps
    cur_txt = "-inf" if cur == float("-inf") else f"{cur:+.1f}"
    if abs(target_db - base) > GAIN_MAX_STEP_DB:
        reachable = max(GAIN_MIN_DB, min(GAIN_MAX_DB, base + math.copysign(GAIN_MAX_STEP_DB, target_db - base)))
        return fail("policy", f"refusing: the fader is at {cur_txt} dB and one call may move it at most "
                              f"{GAIN_MAX_STEP_DB:.0f} dB; the farthest you can set now toward {target_db:+.1f} is {reachable:+.1f}",
                    f"call set_gain again with gain_db={reachable:+.1f}, read list_tracks, then continue if the human asked for more",
                    refused=True)
    if why := _thrash("gain", track_index, track_name, base, target_db):
        return why
    res = _write("set_gain", lambda: server.logic_tracks("set_volume", index=track_index, name=track_name, db=target_db))
    return _remember("gain", track_index, track_name, base, target_db, res)


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
