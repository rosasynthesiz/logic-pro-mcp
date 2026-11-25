"""Locate the running Logic Pro app (desktop purchase or Creator Studio subscription)."""
from __future__ import annotations

import plistlib
import subprocess
from dataclasses import dataclass, asdict
from pathlib import Path

from AppKit import NSWorkspace

BUNDLE_IDS = {
    "com.apple.logic10": "desktop",
    "com.apple.mobilelogic": "creator_studio",
}
INSTALL_PATHS = {
    "desktop": "/Applications/Logic Pro.app",
    "creator_studio": "/Applications/Logic Pro Creator Studio.app",
}


@dataclass
class LogicApp:
    variant: str
    bundle_id: str
    name: str
    path: str
    version: str | None
    pid: int | None

    @property
    def running(self) -> bool:
        return self.pid is not None

    def to_dict(self) -> dict:
        return asdict(self) | {"running": self.running}


def _version_of(path: str) -> str | None:
    try:
        with open(Path(path) / "Contents" / "Info.plist", "rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except Exception:
        return None


def installed() -> list[LogicApp]:
    apps = []
    for variant, path in INSTALL_PATHS.items():
        if Path(path).exists():
            bid = next(b for b, v in BUNDLE_IDS.items() if v == variant)
            apps.append(LogicApp(variant, bid, Path(path).stem, path, _version_of(path), None))
    return apps


def _running_via_ps() -> list[LogicApp]:
    """Find Logic by walking the process table.

    NSWorkspace is the nicer API but it cannot be trusted from a long-lived process: its list of running
    applications is seeded once and then kept current by notifications delivered on a Cocoa run loop. A
    stdio MCP server has no run loop, so its snapshot is frozen at start-up and a Logic launched
    afterwards is invisible to it forever — every tool then reports "Logic not running" while Logic sits
    there on screen. `ps` has no such memory.
    """
    out = []
    try:
        ps = subprocess.run(["ps", "-axo", "pid=,command="], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for variant, path in INSTALL_PATHS.items():
        exe = f"{path}/Contents/MacOS/"
        for line in ps.splitlines():
            line = line.strip()
            if exe not in line:
                continue
            head, _, cmd = line.partition(" ")
            if not cmd.strip().startswith(exe):      # skip helpers and anything merely mentioning it
                continue
            try:
                pid = int(head)
            except ValueError:
                continue
            bid = next(b for b, v in BUNDLE_IDS.items() if v == variant)
            out.append(LogicApp(variant, bid, Path(path).stem, path, _version_of(path), pid))
            break
    return out


def running() -> list[LogicApp]:
    out = []
    for ra in NSWorkspace.sharedWorkspace().runningApplications():
        bid = ra.bundleIdentifier()
        if bid in BUNDLE_IDS:
            path = ra.bundleURL().path() if ra.bundleURL() else INSTALL_PATHS[BUNDLE_IDS[bid]]
            # name = bundle stem so `tell application "<name>"` resolves for both variants
            out.append(LogicApp(BUNDLE_IDS[bid], bid, Path(path).stem, path, _version_of(path), int(ra.processIdentifier())))
    if not out:
        out = _running_via_ps()
    return out


def find(prefer_running: bool = True) -> LogicApp | None:
    """Running app wins; otherwise first installed. Desktop preferred over Creator Studio when both exist."""
    order = {"desktop": 0, "creator_studio": 1}
    pool = sorted(running(), key=lambda a: order[a.variant]) if prefer_running else []
    if not pool:
        pool = sorted(installed(), key=lambda a: order[a.variant])
    return pool[0] if pool else None
