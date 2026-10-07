"""Doctor: which channels are ready, which are blocked, and what to click to unblock them."""
from __future__ import annotations

import json
import sys
import time

from . import target
from .channels import applescript, ax
from .channels.midi import LOGIC_IN_NAME, MCU_PORT, bus


def session_state() -> dict:
    """Locked screen / sleeping display strips app windows out of Accessibility and blanks screen captures."""
    import Quartz as Q
    d = dict(Q.CGSessionCopyCurrentDictionary() or {})
    locked = bool(d.get("CGSSessionScreenIsLocked", False))
    asleep = bool(Q.CGDisplayIsAsleep(Q.CGMainDisplayID()))
    return {"locked": locked, "display_asleep": asleep, "ok": not (locked or asleep)}


def run(probe_mcu: bool = True) -> dict:
    checks: list[dict] = []
    fix: list[str] = []

    sess = session_state()
    checks.append({"name": "session", "status": "ok" if sess["ok"] else "blocked",
                   "summary": "screen unlocked, display awake" if sess["ok"] else f"screen locked={sess['locked']} display asleep={sess['display_asleep']}: Accessibility and pixel readback are unavailable until the Mac is unlocked"})
    if not sess["ok"]:
        fix.append("Unlock the Mac / wake the display")

    app = target.find()
    if app is None:
        checks.append({"name": "logic_app", "status": "failed", "summary": "No Logic Pro install found"})
        fix.append("Install Logic Pro (desktop or Creator Studio)")
    else:
        checks.append({"name": "logic_app", "status": "ok" if app.running else "warn", "summary": f"{app.name} {app.version} [{app.variant}] {'running pid ' + str(app.pid) if app.running else 'not running'}"})
        if not app.running:
            fix.append(f'open -a "{app.name}"')

    # Accessibility
    ax_ok = ax.trusted()
    checks.append({"name": "accessibility", "status": "ok" if ax_ok else "blocked", "summary": "AX trusted" if ax_ok else "Not trusted: System Settings > Privacy & Security > Accessibility > enable this app (Claude / Terminal / python)"})
    if not ax_ok:
        fix.append("Grant Accessibility to the process running logic-mcp")

    # Automation (System Events)
    fm = applescript.frontmost()
    checks.append({"name": "automation_system_events", "status": "ok" if fm else "blocked", "summary": f"frontmost={fm}" if fm else "osascript to System Events denied: Privacy & Security > Automation"})
    if not fm:
        fix.append("Grant Automation > System Events to the process running logic-mcp")

    # CoreMIDI
    ports = bus().list_ports()
    has_in = any(LOGIC_IN_NAME in p for p in ports["outputs"])
    checks.append({"name": "midi_logic_virtual_in", "status": "ok" if has_in else "warn", "summary": f"{LOGIC_IN_NAME} {'present' if has_in else 'missing (is Logic running?)'}"})

    # MCU surface readback
    if probe_mcu:
        b = bus(); b.open_mcu()
        for _ in range(30):  # Logic scans new ports within ~0.5s and we answer its handshake
            if b.mcu.handshake:
                break
            time.sleep(0.1)
        got = b.mcu.handshake
        checks.append({"name": "mcu_surface", "status": "ok" if got else "warn", "summary": f"Mackie Control handshake done, {b.mcu.rx_count} msgs, LCD: {b.mcu.lcd.split(chr(10))[0].strip()[:40]!r}" if got else f"No MCU handshake. In Logic: Settings > Control Surfaces > Setup > New > Install > Mackie Control, set In/Out to '{MCU_PORT}'"})
        if not got:
            fix.append(f"Add Mackie Control surface in Logic pointing at '{MCU_PORT}'")

    ready = [c["name"] for c in checks if c["status"] == "ok"]
    blocked = [c["name"] for c in checks if c["status"] in ("blocked", "failed")]
    return {"app": app.to_dict() if app else None, "checks": checks, "ready": ready, "blocked": blocked, "fix_plan": fix, "python": sys.version.split()[0]}


def main() -> None:
    print(json.dumps(run(), indent=2))


if __name__ == "__main__":
    main()
