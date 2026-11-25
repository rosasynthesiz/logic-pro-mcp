"""Bounce and stem export: drive Logic's dialogs, wait for the render, verify the files on disk."""
from __future__ import annotations

import os
import time
from pathlib import Path

from .channels import ax, ax_logic, dialogs, keys
from .result import fail, ok, unsure

# checkbox AXDescription in the bounce dialog, keyed by the name a caller would use
FORMATS = {
    "wav": "Uncompressed", "aiff": "Uncompressed", "caf": "Uncompressed", "uncompressed": "Uncompressed",
    "mp3": "MP3",
    "m4a": "M4A", "aac": "M4A",
}
FILE_TYPE = {"wav": "WAVE", "aiff": "AIFF", "caf": "CAF"}
EXT = {"Uncompressed": {"WAVE": ".wav", "AIFF": ".aif", "CAF": ".caf"}, "MP3": ".mp3", "M4A": ".m4a"}

# The right-hand options panel swaps with the selected format row, and AXIdentifiers are reused
# across panels, so options are addressed by their visible label instead.
ROW_OPTIONS = {
    "Uncompressed": ("File Type:", "Sample Rate:", "Bit Depth:", "Format:", "Dithering:"),
    "MP3": ("Bit Rate Stereo:", "Bit Rate Mono:", "Stereo Mode:", "Quality:"),
    "M4A": ("Bit Rate:", "Quality:"),
}
SHARED_OPTIONS = ("Mode:", "Normalize:")
ID_TAIL, ID_ADD_TO_PROJECT = "_NS:30", "_NS:731"


def _fmt_checkbox(dlg, label: str):
    return ax.find(dlg, role_="AXCheckBox", desc_=label, max_depth=6)


def format_rows(dlg) -> dict:
    """{'Uncompressed': row, 'MP3': row, ...} — the table rows that swap the options panel."""
    out = {}
    for r in ax.find_all(dlg, lambda e: ax.role(e) == "AXRow", max_depth=6, limit=8):
        txt = [str(ax.value(t)) for t in ax.find_all(r, lambda e: ax.role(e) == "AXStaticText", max_depth=3, limit=2)]
        if txt:
            out[txt[0].split(":")[0].strip()] = r
    return out


def select_format_row(dlg, label: str, settle: float = 0.5) -> bool:
    """Click a format row so its options appear in the right-hand panel."""
    from .channels import mouse, screen
    row = format_rows(dlg).get(label)
    if row is None:
        return False
    if ax.attr(row, "AXSelected"):
        return True
    r = screen.element_rect(row)
    if r is None:
        return False
    mouse.click(r[0] + r[2] * 0.6, r[1] + r[3] / 2, pid=None)
    time.sleep(settle)
    return bool(ax.attr(row, "AXSelected"))


def _requires_text(dlg) -> str | None:
    for el in ax.find_all(dlg, lambda e: ax.role(e) == "AXStaticText", max_depth=3, limit=30):
        v = str(ax.value(el) or "")
        if v.startswith("Requires"):
            return v
    return None


def open_bounce(root, timeout: float = 4.0):
    if not ax_logic.press_menu_item(root, "File", "Bounce", "~^Project or Section"):
        return None
    return dialogs.wait_for(lambda: next((x for x in dialogs.other_windows(root) if str(ax.title(x) or "").startswith("Bounce")), None), timeout)


def bounce(
    root, pid: int, app_name: str,
    directory: str,
    filename: str,
    formats: list[str],
    start_bar: int | None = None,
    end_bar: int | None = None,
    sample_rate: str | None = None,
    bit_depth: str | None = None,
    normalize: str | None = None,
    mode: str | None = None,
    include_tail: bool | None = None,
    timeout: float = 900.0,
) -> dict:
    """Bounce the project (or a bar range) to one or more formats in a single pass, then verify the files."""
    wanted = []
    for f in formats:
        key = f.strip().lower().lstrip(".")
        if key not in FORMATS:
            return fail("bounce", f"unknown format {f!r}", f"use one of {sorted(set(FORMATS))}")
        wanted.append((key, FORMATS[key]))
    labels = {lbl for _, lbl in wanted}

    directory = os.path.expanduser(directory)
    if not os.path.isdir(directory):
        return fail("bounce", f"destination is not a directory: {directory}")
    stem = Path(filename).stem

    # what should appear on disk when this succeeds
    ftype = FILE_TYPE.get(next((k for k, l in wanted if l == "Uncompressed"), "wav"), "WAVE")
    expect = []
    for _, lbl in wanted:
        e = EXT[lbl][ftype] if lbl == "Uncompressed" else EXT[lbl]
        p = os.path.join(directory, stem + e)
        if p not in expect:
            expect.append(p)
    pre = {p: (os.path.getmtime(p) if os.path.exists(p) else None) for p in expect}

    if dialogs.other_windows(root):
        return fail("bounce", "another dialog is already open", {"windows": ax_logic.window_titles(root)})

    ax_logic.raise_app(root, ax_logic.main_window(root), pid); time.sleep(0.3)
    dlg = open_bounce(root)
    if dlg is None:
        return fail("menu", "Bounce dialog did not open")

    steps: dict = {}
    try:
        # bar range first: it changes the size estimate
        for label, val in (("Start:", start_bar), ("End:", end_bar)):
            if val is None:
                continue
            g = dialogs.segment_group(dlg, label)
            if g is None:
                steps[label] = {"ok": False, "reason": "field not found"}
            else:
                steps[label] = dialogs.set_segments(g, [val, 1, 1, 1], pid)

        for lbl in ("Uncompressed", "MP3", "M4A"):
            cb = _fmt_checkbox(dlg, lbl)
            if cb is None:
                steps[lbl] = {"ok": False, "reason": "checkbox not found"}
                continue
            steps[lbl] = ax_logic.set_checkbox(cb, lbl in labels)

        # per-format options live behind that format's row
        uncompressed_key = next((k for k, l in wanted if l == "Uncompressed"), None)
        want = {"Uncompressed": {"File Type:": FILE_TYPE.get(uncompressed_key), "Sample Rate:": sample_rate, "Bit Depth:": bit_depth}}
        for lbl in labels:
            opts = {k: v for k, v in want.get(lbl, {}).items() if v}
            if not opts:
                continue
            if not select_format_row(dlg, lbl):
                steps[f"select {lbl}"] = {"ok": False, "reason": "row not selectable"}
                continue
            for label, val in opts.items():
                steps[f"{lbl} {label}"] = dialogs.set_popup(dialogs.popup_for_label(dlg, label), val, pid)
        for label, val in (("Mode:", mode), ("Normalize:", normalize)):
            if val:
                steps[label] = dialogs.set_popup(dialogs.popup_for_label(dlg, label), val, pid)
        if include_tail is not None:
            cb = dialogs.find_by_id(dlg, ID_TAIL)
            if cb is not None:
                steps["include_tail"] = ax_logic.set_checkbox(cb, include_tail)

        steps["requires"] = _requires_text(dlg)
        bad = [k for k, v in steps.items() if isinstance(v, dict) and not v.get("ok")]
        if bad:
            dialogs.cancel_all(root)
            return fail("bounce", f"could not set: {bad}", {"steps": steps})

        okb = dialogs.button(dlg, "OK", max_depth=3)
        if okb is None or not ax.press(okb):
            dialogs.cancel_all(root)
            return fail("bounce", "OK button not pressable")

        panel = dialogs.save_panel(root)
        if panel is None:
            dialogs.cancel_all(root)
            return fail("bounce", "save panel did not appear", {"windows": ax_logic.window_titles(root)})

        ax_logic.raise_app(root, ax_logic.main_window(root), pid); time.sleep(0.5)
        steps["directory"] = dialogs.set_directory(panel, pid, directory)
        if not steps["directory"]["ok"]:
            # Never bounce into whatever folder the panel happened to open in. Measured: a refused
            # directory step let the render target an old scratch folder and stall on a Replace? sheet.
            dialogs.cancel_all(root)
            return fail("bounce", "could not set the destination folder", {"steps": steps})
        steps["filename"] = dialogs.set_filename(panel, stem, pid)
        if not steps["filename"]["ok"]:
            dialogs.cancel_all(root)
            return fail("bounce", "could not set filename", {"steps": steps})

        if not dialogs.confirm_save(panel, app_name):
            dialogs.cancel_all(root)
            return fail("bounce", "Bounce button not pressable")
    except Exception as e:  # never leave a modal dialog on screen
        dialogs.cancel_all(root)
        return fail("bounce", f"{type(e).__name__}: {e}", {"steps": steps})

    # an alert can appear here (existing file, missing folder, region overlap)
    a = dialogs.answer_alert(root, choice="Replace", timeout=1.5)
    if a and not a["pressed"]:
        dialogs.cancel_all(root)
        return fail("bounce", f"unexpected alert: {a['text']}", {"buttons": a["buttons"]})
    if a:
        steps["alert"] = a

    # wait for every expected file to appear and stop growing
    deadline = time.time() + timeout
    stable: dict = {}
    while time.time() < deadline:
        done = True
        for p in expect:
            if not os.path.exists(p) or os.path.getmtime(p) == pre[p]:
                done = False; stable.pop(p, None); continue
            sz = os.path.getsize(p)
            if stable.get(p) != sz or sz == 0:
                stable[p] = sz; done = False
        if done:
            break
        time.sleep(1.0)

    produced = [{"path": p, "bytes": os.path.getsize(p)} for p in expect if os.path.exists(p) and os.path.getmtime(p) != pre[p]]
    leftover = ax_logic.window_titles(root)
    detail = {"files": produced, "expected": expect, "steps": steps, "windows": leftover}
    if len(produced) == len(expect):
        return ok("bounce", f"bounced {len(produced)} file(s) to {directory}", detail)
    if produced:
        return unsure("bounce", f"{len(produced)} of {len(expect)} files appeared", detail)
    return unsure("bounce", "no files appeared before the timeout; the render may still be running", detail)


def export_stems(root, pid: int, app_name: str, directory: str, timeout: float = 900.0) -> dict:
    """File > Export > All Tracks as Audio Files… — one stem per track, the input for level analysis."""
    directory = os.path.expanduser(directory)
    os.makedirs(directory, exist_ok=True)
    before = set(os.listdir(directory))
    if dialogs.other_windows(root):
        return fail("export", "another dialog is already open", {"windows": ax_logic.window_titles(root)})
    ax_logic.raise_app(root, ax_logic.main_window(root), pid); time.sleep(0.3)
    if not ax_logic.press_menu_item(root, "File", "Export", "~All Tracks as Audio Files"):
        return fail("menu", "Export > All Tracks as Audio Files… not found")
    panel = dialogs.wait_for(lambda: next((x for x in dialogs.other_windows(root)), None), 4.0)
    if panel is None:
        return fail("export", "panel did not appear")
    try:
        ax_logic.raise_app(root, ax_logic.main_window(root), pid); time.sleep(0.5)
        dialogs.set_directory(panel, pid, directory)
        if not dialogs.confirm_save(panel, app_name):
            dialogs.cancel_all(root)
            return fail("export", "could not confirm the panel")
    except Exception as e:
        dialogs.cancel_all(root)
        return fail("export", f"{type(e).__name__}: {e}")

    deadline = time.time() + timeout
    sizes: dict = {}
    while time.time() < deadline:
        now = set(os.listdir(directory)) - before
        cur = {n: os.path.getsize(os.path.join(directory, n)) for n in now}
        if cur and cur == sizes and all(v > 0 for v in cur.values()) and not dialogs.other_windows(root):
            break
        sizes = cur
        time.sleep(1.5)
    files = [{"path": os.path.join(directory, n), "bytes": s} for n, s in sorted(sizes.items())]
    if files:
        return ok("export", f"exported {len(files)} stem(s) to {directory}", {"files": files})
    return unsure("export", "no stems appeared before the timeout", {"directory": directory, "windows": ax_logic.window_titles(root)})
