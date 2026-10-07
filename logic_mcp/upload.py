"""Upload rendered audio to the user's own cloud storage, via rclone.

rclone is used rather than a Drive API client because bounced audio is large and binary; rclone streams
it straight from disk instead of pushing base64 through the model's context.

WHOSE ACCOUNT
-------------
This server ships with **no credentials and no default remote**. Development used a personal Google Drive
remote, and shipping that would have uploaded every user's audio into someone else's Drive. So:

* `DEFAULT_REMOTE` is None. A remote is chosen, in order, from the caller's argument, the
  `LOGIC_MCP_REMOTE` environment variable, or — only when there is exactly one and it is unambiguous —
  the single remote the user has configured. Two or more configured remotes with nothing pinned is an
  error that names them, never a guess.
* `setup_hint()` returns the exact commands for the user to run themselves. Authorisation happens in
  rclone's own browser flow, under the user's own Google account. This process never sees, stores, asks
  for or transports a token, password or client secret, and it never runs `rclone config` on the user's
  behalf.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess

from .result import fail, ok, unsure

RCLONE = shutil.which("rclone") or os.path.expanduser("~/bin/rclone")
REMOTE_ENV = "LOGIC_MCP_REMOTE"
DEFAULT_REMOTE = None                    # deliberately unset — see the module docstring


def available() -> bool:
    return bool(RCLONE and os.path.exists(RCLONE))


def setup_hint(remote_name: str = "drive") -> dict:
    """What the USER must run to connect their own Drive. Never run these for them: the flow ends in a
    browser sign-in to their Google account, and their credentials are theirs to enter."""
    return {
        "rclone_installed": available(),
        "install": "brew install rclone",
        "connect": f"rclone config create {remote_name} drive scope=drive",
        "note": ("rclone opens your browser to sign in to your own Google account and stores the token in "
                 "your own rclone config. Nothing is sent to this server, and no credentials are stored here."),
        "then": f"set {REMOTE_ENV}={remote_name}, or pass remote=\"{remote_name}\" on each call",
        "verify": "rclone about " + remote_name + ":",
    }


def resolve_remote(remote: str | None = None) -> tuple[str | None, str | None]:
    """(remote, error). Explicit argument, then $LOGIC_MCP_REMOTE, then the single configured remote.

    Never guesses between several remotes: picking the wrong one uploads a client's unreleased audio to
    the wrong account, and that is not undoable.
    """
    configured = remotes()
    if remote:
        if remote not in configured:
            return None, f"no rclone remote named {remote!r}; configured: {configured or 'none'}"
        return remote, None
    pinned = os.environ.get(REMOTE_ENV, "").strip()
    if pinned:
        if pinned not in configured:
            return None, f"${REMOTE_ENV} is {pinned!r} but rclone has no such remote; configured: {configured or 'none'}"
        return pinned, None
    if not configured:
        return None, "no rclone remote is configured — connect your own cloud account first"
    if len(configured) > 1:
        return None, (f"several remotes are configured ({configured}); pass remote=... or set ${REMOTE_ENV} "
                      "— refusing to guess which account your audio should go to")
    return configured[0], None


def _run(args: list[str], timeout: float = 600.0) -> tuple[int, str, str]:
    p = subprocess.run([RCLONE, *args], capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout.strip(), p.stderr.strip()


def remotes() -> list[str]:
    if not available():
        return []
    code, out, _ = _run(["listremotes"], timeout=20)
    return [r.rstrip(":") for r in out.splitlines() if r.strip()] if code == 0 else []


def about(remote: str) -> dict:
    code, out, err = _run(["about", f"{remote}:", "--json"], timeout=30)
    if code != 0:
        return {"ok": False, "error": err[-400:]}
    try:
        return {"ok": True, **json.loads(out)}
    except json.JSONDecodeError:
        return {"ok": False, "error": out[-400:]}


def listing(remote: str, folder: str = "") -> dict:
    code, out, err = _run(["lsjson", f"{remote}:{folder}"], timeout=60)
    if code != 0:
        return {"ok": False, "error": err[-400:]}
    try:
        items = json.loads(out)
    except json.JSONDecodeError:
        return {"ok": False, "error": out[-400:]}
    return {"ok": True, "items": [{"name": i.get("Name"), "bytes": i.get("Size"), "dir": i.get("IsDir")} for i in items]}


def link(remote: str, path: str) -> str | None:
    code, out, _ = _run(["link", f"{remote}:{path}"], timeout=60)
    return out if code == 0 and out.startswith("http") else None


def upload(paths: list[str], remote: str | None = None, folder: str = "", make_links: bool = True,
           timeout: float = 1800.0) -> dict:
    """Copy local files to remote:folder, then verify each landed with the right size."""
    if not available():
        return fail("rclone", "rclone is not installed", setup_hint())

    remote, err = resolve_remote(remote)
    if err:
        return fail("rclone", err, setup_hint())

    missing = [p for p in paths if not os.path.exists(p)]
    if missing:
        return fail("rclone", f"local files missing: {missing}")
    want = {os.path.basename(p): os.path.getsize(p) for p in paths}

    dest = f"{remote}:{folder}" if folder else f"{remote}:"
    for p in paths:
        code, _, err = _run(["copy", p, dest, "--no-traverse"], timeout=timeout)
        if code != 0:
            return fail("rclone", f"upload failed for {os.path.basename(p)}", {"stderr": err[-500:]})

    got = listing(remote, folder)
    if not got.get("ok"):
        return unsure("rclone", "uploaded but could not list the destination", got)
    remote_sizes = {i["name"]: i["bytes"] for i in got["items"] if not i["dir"]}

    files = []
    bad = []
    for name, size in want.items():
        rsize = remote_sizes.get(name)
        entry = {"name": name, "local_bytes": size, "remote_bytes": rsize}
        if rsize != size:
            bad.append(name)
        elif make_links:
            entry["link"] = link(remote, f"{folder}/{name}" if folder else name)
        files.append(entry)

    detail = {"remote": remote, "folder": folder, "files": files}
    if bad:
        return unsure("rclone", f"size mismatch for {bad}", detail)
    return ok("rclone", f"uploaded {len(files)} file(s) to {dest}", detail)
