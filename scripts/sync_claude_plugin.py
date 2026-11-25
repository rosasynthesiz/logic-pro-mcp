#!/usr/bin/env python3
"""Refresh claude-plugin/logic_mcp from the repo's logic_mcp package.

The Claude plugin is submitted on its own (only the claude-plugin/ folder), so it carries a vendored copy
of the modules the MCP server imports at runtime. Run this after changing logic_mcp, then commit both.

    python scripts/sync_claude_plugin.py          # copy
    python scripts/sync_claude_plugin.py --check  # exit 1 if the vendored copy has drifted
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "logic_mcp"
DST = ROOT / "claude-plugin" / "logic_mcp"

# Everything the server (logic_mcp.server) and the doctor import at runtime. Not vendored on purpose: mix.py
# (+ mix_recipes.json), dialogue, cuewav, organise, instruments, the gesture daemons and handd, the layouts/
# data and osc_map.txt, which the server never loads.
INCLUDED = [
    "__init__.py",
    "audio.py",
    "doctor.py",
    "export.py",
    "leveling.py",
    "markers.py",
    "plugins.py",
    "projectdata.py",
    "result.py",
    "server.py",
    "target.py",
    "upload.py",
    "channels/__init__.py",
    "channels/applescript.py",
    "channels/ax.py",
    "channels/ax_logic.py",
    "channels/dialogs.py",
    "channels/keys.py",
    "channels/midi.py",
    "channels/mouse.py",
    "channels/osc.py",
    "channels/palette.py",
    "channels/screen.py",
    "gesture/__init__.py",
    "gesture/padd.py",
    "gesture/protocol.py",
]


def vendored_files() -> list[str]:
    return sorted(str(p.relative_to(DST)) for p in DST.rglob("*") if p.is_file() and "__pycache__" not in p.parts)


def drift() -> list[str]:
    problems = []
    for rel in INCLUDED:
        s, d = SRC / rel, DST / rel
        if not d.exists():
            problems.append(f"missing in plugin: {rel}")
        elif s.read_bytes() != d.read_bytes():
            problems.append(f"differs: {rel}")
    for rel in vendored_files():
        if rel not in INCLUDED:
            problems.append(f"unexpected file in plugin: {rel}")
    return problems


def sync() -> None:
    if DST.exists():
        shutil.rmtree(DST)
    for rel in INCLUDED:
        (DST / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(SRC / rel, DST / rel)


if __name__ == "__main__":
    if "--check" in sys.argv:
        found = drift()
        print("\n".join(found) or "claude-plugin/logic_mcp is in sync")
        sys.exit(1 if found else 0)
    sync()
    print(f"synced {len(INCLUDED)} files into {DST.relative_to(ROOT)}")
