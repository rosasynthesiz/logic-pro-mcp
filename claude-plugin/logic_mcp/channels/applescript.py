"""AppleScript channel. Logic has no scripting dictionary, so this is lifecycle + System Events only."""
from __future__ import annotations

import subprocess


def _q(value: str) -> str:
    """Escape a value for use inside an AppleScript string literal.

    Backslash and double quote are the only characters AppleScript treats specially there. Without
    this, a project called `Mix "final".logicx` ends the literal early and whatever follows runs as
    AppleScript -- with the caller's words, under the user's account.
    """
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def run(script: str, timeout: float = 10.0) -> tuple[bool, str]:
    try:
        p = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "osascript timeout"
    return p.returncode == 0, (p.stdout if p.returncode == 0 else p.stderr).strip()


def activate(app_name: str) -> tuple[bool, str]:
    return run(f'tell application "{_q(app_name)}" to activate')


def ensure_front(app_name: str, timeout: float = 3.0, step: float = 0.15) -> bool:
    """Activate and WAIT until the app really is frontmost.

    HID clicks and key events go to whatever is in front, so acting without confirming this silently
    delivers them to another application — measured: clicks meant for Logic landed on the terminal.
    """
    import time
    if frontmost() == app_name:
        return True
    activate(app_name)
    time.sleep(0.35)                 # activation is asynchronous; give it a beat before polling
    end = time.time() + timeout
    while time.time() < end:
        if frontmost() == app_name:
            return True
        time.sleep(step)
    return frontmost() == app_name


def quit_app(app_name: str) -> tuple[bool, str]:
    return run(f'tell application "{_q(app_name)}" to quit')


def open_file(app_name: str, path: str) -> tuple[bool, str]:
    return run(f'tell application "{_q(app_name)}" to open POSIX file "{_q(path)}"')


def is_running(app_name: str) -> bool:
    ok, out = run(f'tell application "System Events" to (name of processes) contains "{_q(app_name)}"')
    return ok and out == "true"


def frontmost() -> str | None:
    ok, out = run('tell application "System Events" to get name of first application process whose frontmost is true')
    return out if ok else None


def press_menu(app_name: str, *path: str) -> tuple[bool, str]:
    """Click a menu item by path, e.g. press_menu(app, "Track", "New Software Instrument Track")."""
    if len(path) < 2:
        return False, "need menu + item"
    menu, *items = path
    chain = f'menu bar item "{_q(menu)}" of menu bar 1'
    for it in items[:-1]:
        chain = f'menu item "{_q(it)}" of menu 1 of {chain}'
    target = f'menu item "{_q(items[-1])}" of menu 1 of {chain}'
    return run(f'tell application "System Events" to tell process "{_q(app_name)}" to click {target}')
