"""The local profile over stdio, wired to the fake adapters in tests/fakeax.py instead of Logic.

This is the server half of the model evaluation (docs/local-model.md, acceptance gate step 2). It is
the SAME `logic_mcp.local` server, tools/list and tools/call included; only the modules that talk to
macOS are replaced: Accessibility becomes the element tree from tests/fakeax, MIDI is a Mock, osascript
never runs, sleeps are skipped, the mutation guard uses a private lock file. Nothing here can reach a
running Logic, a MIDI port or the OSC surface, which is the point: a model can be let loose on it.

The scene is described by one JSON object in the LOCAL_EVAL_SCENARIO environment variable (or a file
named by LOCAL_EVAL_SCENARIO_FILE). Every key is optional:

    tracks          ["Vocal", "Guitar"] or [{"name": "Vocal", "volume_db": -6, "muted": true,
                    "soloed": false, "armed": false}, ...]. Names may repeat.
    tempo           120            playing  false         title/doc   the window identity
    locked          false          logic_running true     alert       null or an alert title
    sheet           false          trusted  true
    inert           ["play", "stop", "tempo", "mute:1", "volume:2"]  controls Logic "ignores", so a
                    write comes back uncertain instead of confirmed (mute:N / volume:N are 1-based)
    stale           {"after_tool": "list_tracks", "tracks": [...]}: once that tool has answered, the
                    track list is swapped for the new one (keeping mute/volume state of surviving names),
                    so whatever the caller copied is stale.

Run it by hand to poke at it:

    LOCAL_EVAL_SCENARIO='{"tracks": ["Vocal", "Guitar"], "locked": true}' \
        uv run python scripts/local_fake_server.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

WRITE_TOOLS = ("play", "stop", "set_tempo", "set_mute", "set_gain")


class _Patcher:
    """The two methods of pytest's monkeypatch that tests/fakeax.wire uses."""

    def setattr(self, obj, name, value):
        setattr(obj, name, value)

    def setitem(self, mapping, key, value):
        mapping[key] = value


def load_scenario() -> dict:
    raw = os.environ.get("LOCAL_EVAL_SCENARIO")
    path = os.environ.get("LOCAL_EVAL_SCENARIO_FILE")
    if raw:
        return json.loads(raw)
    if path:
        return json.loads(Path(path).read_text())
    return {}


def _track_specs(items) -> list[dict]:
    out = []
    for t in items or ["Vocal", "Guitar"]:
        if isinstance(t, str):
            out.append({"name": t})
        else:
            out.append(dict(t))
    return out


def _db_text(v) -> str:
    if v is None:
        return "+0.0 dB"
    if isinstance(v, str):
        return v
    if v == float("-inf") or v <= -100:
        return "-∞ dB"
    return f"{float(v):+.1f} dB"


def _apply_track_states(fakeax, win, specs: list[dict], keep: dict | None = None) -> None:
    """Set mute/solo/arm/volume on each header from the spec, or from `keep` (name -> state) for a swap."""
    from logic_mcp.channels import ax_logic
    for item, spec in zip(ax_logic.track_items(win), specs):
        prev = (keep or {}).get(spec["name"])
        for key, label in (("muted", "Mute"), ("soloed", "Solo"), ("armed", "Record Enable")):
            el = next(c for c in item.children if c.desc == label)
            if key in spec:
                el.value = 1 if spec[key] else 0
            elif prev is not None:
                el.value = prev[key]
        vol = next(c for c in item.children if c.desc == "Volume")
        if "volume_db" in spec:
            vol.attrs["AXValueDescription"] = _db_text(spec["volume_db"])
        elif prev is not None:
            vol.attrs["AXValueDescription"] = prev["volume_text"]


def _snapshot(win) -> dict:
    from logic_mcp.channels import ax_logic
    out = {}
    for item in ax_logic.track_items(win):
        info = ax_logic.track_info(item)
        out[info["name"]] = {"muted": 1 if info["mute"] else 0, "soloed": 1 if info["solo"] else 0,
                             "armed": 1 if info["arm"] else 0, "volume_text": info["volume_text"]}
    return out


def wire_scenario(scenario: dict):
    """Patch every module that talks to Logic so the local profile runs against the described scene.

    Returns the SimpleNamespace from tests/fakeax.wire (root, win, ax, midi, app, target)."""
    from tests import fakeax
    from logic_mcp import doctor, local, server
    from logic_mcp.channels import ax_logic

    specs = _track_specs(scenario.get("tracks"))
    kw = {}
    if "title" in scenario:
        kw["title"] = scenario["title"]
    if "doc" in scenario:
        kw["doc"] = scenario["doc"]
    L = fakeax.wire(_Patcher(), tuple(s["name"] for s in specs), **kw)
    _apply_track_states(fakeax, L.win, specs)

    bar = L.ax.find(L.win, role_="AXGroup", desc_="Control Bar")
    play_box = L.ax.find(bar, title_="Play")
    tempo = L.ax.find(bar, role_="AXSlider", desc_="Tempo")
    tempo.value = float(scenario.get("tempo", 120))
    play_box.value = 1 if scenario.get("playing") else 0

    inert = set(scenario.get("inert") or [])
    if "play" in inert:
        play_box.inert = True
    if "tempo" in inert:
        tempo.inert = True
    for tag in inert:
        kind, _, idx = tag.partition(":")
        if kind in ("mute", "volume") and idx.isdigit():
            items = ax_logic.track_items(L.win)
            i = int(idx) - 1
            if 0 <= i < len(items):
                label = "Mute" if kind == "mute" else "Volume"
                next(c for c in items[i].children if c.desc == label).inert = True

    # Space toggles playback the way Logic does, unless the scene says Logic ignores the key. The real
    # keys module is a Mock after wire(); this gives its press() an effect so `stop` can confirm.
    def press(chord, pid=None):
        if chord == server.keys.LOGIC_KEYS["play_stop"] and "stop" not in inert and not play_box.inert:
            play_box.value = 0 if play_box.value else 1
        return True
    server.keys.press = press

    if scenario.get("locked"):
        doctor.session_state = lambda: {"locked": True, "display_asleep": False, "ok": False}
    if scenario.get("logic_running") is False:
        L.target.find.return_value = fakeax.logic_app(pid=None)
        L.root.children.clear()                     # no process, no windows: list_tracks has nothing to read
    if scenario.get("trusted") is False:
        L.ax._trusted = False
    if scenario.get("alert"):
        L.root.children.append(fakeax.El("AXWindow", desc="alert", title=str(scenario["alert"])))
    if scenario.get("sheet"):
        L.win.children.append(fakeax.El("AXSheet"))

    stale = scenario.get("stale")
    if stale:
        fired = {"done": False}
        trigger = stale.get("after_tool", "list_tracks")
        new_specs = _track_specs(stale.get("tracks"))
        real_call = local.call

        def call(name, arguments):
            res = real_call(name, arguments)
            if not fired["done"] and name == trigger:
                fired["done"] = True
                keep = _snapshot(L.win)
                header = L.ax.find(L.win, role_="AXGroup", desc_="Tracks header")
                header.children = [fakeax.track_item(i + 1, s["name"]) for i, s in enumerate(new_specs)]
                _apply_track_states(fakeax, L.win, new_specs, keep)
            return res
        local.call = call
    return L


def main() -> None:
    # A private lock: the eval must never contend with, or hold up, a real logic-pro-mcp of this user.
    os.environ.setdefault("LOGIC_MCP_LOCK", os.path.join(tempfile.gettempdir(), f"logic-pro-mcp-fake-{os.getpid()}.lock"))
    scenario = load_scenario()
    from logic_mcp import local, policy
    policy.guard = policy.MutationGuard(lock_path=os.environ["LOGIC_MCP_LOCK"], timeout=2.0)
    wire_scenario(scenario)
    local.main()


if __name__ == "__main__":
    main()
