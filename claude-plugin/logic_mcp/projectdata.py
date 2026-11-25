"""Read and rename inside Logic's ProjectData binary without opening Logic.

Verified live on Logic Pro Creator Studio 12.3 (2026-09-05). Every number below was measured by diffing
pairs of Logic's own saves that differ by exactly one rename, then confirmed by patching a copy, closing
and reopening it in Logic, and reading the names back. A file carrying four renames at once (a track
grown 8 bytes, another shrunk 8, a group grown 4, a folder grown 4) opened clean and showed all four.

Two record families carry names:

  TRACK family (audio tracks, folder/summing stacks, Master, buses):
      marker:2 | 00 00 00 00 | type:2 | 00 00 | LEN:u16 | name padded to an EVEN byte count | ...
      u32 @ name-168 = 463 + slot                    (slot = LEN rounded up to even)
      type 8010 = audio track, 0014 = folder/stack + Master, 8040/8008 = other track kinds
  NAMED family (class tag "MSeq", stored byte-reversed as 71 65 53 4d at name-54):
      ... u32 @ name-26 = 297 + slot ... 70 03 01 ... | LEN:u16 | name padded to even | ...
      The MIXER GROUPS are members of this family, and so are automation lanes, sequences, clip
      templates, loop files and the project name. Nothing in the bytes around the name separates them
      (measured: identical prefixes and class tag). Position does: the mixer groups are one contiguous
      run (gaps under 2.2 KB measured) ending just before the track table, while every other member sits
      190 KB earlier or after the track table. records() keeps kind 'group' only for that run and labels
      the rest 'named'; rename() never touches 'named' unless asked for it by kind.

  Common:        u32 @ file offset 16 = filesize - 24.

  Nothing else needs updating. Six u16 fields early in the file were seen to move by the same delta as
  one folder rename and were suspected to be a size table; they turned out to be a cache Logic rebuilds
  on load -- a Logic-made rename of a middle folder left them unchanged, and a patched rename of the
  last folder that left them untouched opened clean and showed the new name. They are ignored.

Records are packed, so a slot change shifts every later byte; that is safe only because the file holds
no absolute offsets (scanned). Every patch is re-validated (check_layout, and the renamed record must be
found again at the same offset under the same kind) before it is returned -- but the only proof that
Logic agrees is Logic: never write the live project. Patch a copy, CLOSE and reopen it in Logic (Revert
is a silent no-op when nothing is unsaved), and read the names back.
"""
from __future__ import annotations

import struct

HEADER_LEN_OFF = 16
TRACK_BACK, TRACK_BASE = 168, 463
NAMED_BACK, NAMED_BASE = 26, 297
NAMED_TAG = b"\x70\x03\x01"            # at name-18 in every NAMED-family record measured
FOLDER_TYPES = {"0014"}
MAX_NAME = 64
GROUP_RUN_GAP = 4096                   # measured max gap inside the mixer-group run: 2089
GROUP_TABLE_TO_TRACKS_MAX = 16384      # measured: last group -> first track record was 4193


def slot(n: int) -> int:
    return (n + 1) // 2 * 2


def _decode(b: bytes) -> str | None:
    """UTF-8 text with no control characters, else None. Names are UTF-8, so Tamil and accented names
    are multi-byte; an ASCII-only gate made such records vanish from the scan (review finding)."""
    try:
        t = b.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return t if t and all(ord(c) >= 32 and ord(c) != 127 for c in t) else None


def records(data: bytes) -> list[dict]:
    """Every name record of both families, in file order.

    kind: 'track' | 'folder' | 'group' | 'named' | 'other'. A record is patchable only when its length
    field already obeys the measured formula; structurally matching records that fail it, or whose name
    is not valid UTF-8, are listed as 'other' (with 'undecodable': True in the latter case) so they are
    visible rather than silently skipped.
    """
    out, i = [], 10
    n = len(data)
    while i < n - MAX_NAME - 2:
        ln = data[i] | (data[i + 1] << 8)
        if 1 <= ln <= MAX_NAME:
            ns = i + 2
            raw = data[ns:ns + ln]
            pad_ok = data[ns + ln:ns + slot(ln)] == b"\x00" * (slot(ln) - ln)
            track_shape = (data[i - 8:i - 4] == b"\x00" * 4 and data[i - 2:i] == b"\x00\x00"
                           and data[i - 10:i - 8] != b"\x00\x00" and ns >= TRACK_BACK)
            named_shape = data[ns - 18:ns - 15] == NAMED_TAG and ns >= NAMED_BACK
            if pad_ok and (track_shape or named_shape):
                name = _decode(raw)
                rec = {"name_off": ns, "len": ln, "name": name if name is not None else raw.decode("latin-1"),
                       "undecodable": name is None}
                if track_shape:
                    typ = data[i - 4:i - 2].hex()
                    ok = struct.unpack_from("<I", data, ns - TRACK_BACK)[0] == TRACK_BASE + slot(ln)
                    rec["type"] = typ
                    rec["kind"] = ("folder" if typ in FOLDER_TYPES else "track") if ok and name else "other"
                else:
                    ok = struct.unpack_from("<I", data, ns - NAMED_BACK)[0] == NAMED_BASE + slot(ln)
                    rec["type"] = "named"
                    rec["kind"] = "named" if ok and name else "other"
                out.append(rec)
                i = ns + slot(ln)
                continue
        i += 1
    return _classify_groups(out)


def _classify_groups(recs: list[dict]) -> list[dict]:
    """Promote the mixer-group table from 'named' to 'group'.

    Candidate runs are maximal sequences of NAMED-family records with gaps under GROUP_RUN_GAP. The
    mixer-group table is the run that a track record follows most closely (within
    GROUP_TABLE_TO_TRACKS_MAX). Anchoring on the nearest following track, rather than on the first track
    in the whole file, means a stray early track record cannot erase the table (review finding), and
    the tight gap keeps unrelated family members from being absorbed.
    """
    tracks = sorted(r["name_off"] for r in recs if r["kind"] in ("track", "folder"))
    named = [r for r in recs if r["kind"] == "named"]
    if not tracks or not named:
        return recs
    runs: list[list[dict]] = []
    for r in named:
        if runs and r["name_off"] - runs[-1][-1]["name_off"] <= GROUP_RUN_GAP:
            runs[-1].append(r)
        else:
            runs.append([r])
    import bisect
    best, best_gap = None, None
    for run in runs:
        end = run[-1]["name_off"]
        k = bisect.bisect_right(tracks, end)
        if k == len(tracks):
            continue
        gap = tracks[k] - end
        if gap <= GROUP_TABLE_TO_TRACKS_MAX and (best_gap is None or gap < best_gap):
            best, best_gap = run, gap
    if best:
        for r in best:
            r["kind"] = "group"
    return recs


def check_layout(data: bytes) -> list[str]:
    """Empty when the header and every patchable record's length field agree with the file."""
    bad = []
    hdr = struct.unpack_from("<I", data, HEADER_LEN_OFF)[0]
    if hdr != len(data) - 24:
        bad.append(f"header {hdr} != filesize-24 {len(data) - 24}")
    for r in records(data):
        if r["kind"] in ("track", "folder"):
            v = struct.unpack_from("<I", data, r["name_off"] - TRACK_BACK)[0]
            if v != TRACK_BASE + slot(r["len"]):
                bad.append(f"{r['name']}: track length {v} != {TRACK_BASE + slot(r['len'])}")
        elif r["kind"] in ("group", "named"):
            v = struct.unpack_from("<I", data, r["name_off"] - NAMED_BACK)[0]
            if v != NAMED_BASE + slot(r["len"]):
                bad.append(f"{r['name']}: named length {v} != {NAMED_BASE + slot(r['len'])}")
    return bad


def rename(data: bytes, old: str, new: str, kind: str | None = None) -> bytes:
    """Return a patched copy with one record renamed.

    kind: 'track' | 'folder' | 'group' | 'named'. Without kind, 'named' records (automation lanes,
    sequences, the project name...) are never touched, so a group rename cannot land on bookkeeping.
    """
    nb = new.encode()
    if not 1 <= len(nb) <= MAX_NAME:
        raise ValueError(f"name must be 1..{MAX_NAME} UTF-8 bytes")
    if _decode(nb) is None:
        raise ValueError("name must be printable UTF-8")
    hits = [r for r in records(data) if r["name"] == old and r["kind"] != "other"
            and (r["kind"] == kind if kind else r["kind"] != "named")]
    if len(hits) != 1:
        raise ValueError(f"expected exactly one patchable record named {old!r}"
                         + (f" of kind {kind}" if kind else "") + f", found {len(hits)}")
    r = hits[0]
    ns, old_slot, new_slot = r["name_off"], slot(r["len"]), slot(len(nb))
    delta = new_slot - old_slot
    out = bytearray(data[:ns]) + bytearray(nb.ljust(new_slot, b"\x00")) + bytearray(data[ns + old_slot:])
    struct.pack_into("<H", out, ns - 2, len(nb))
    back = NAMED_BACK if r["kind"] in ("group", "named") else TRACK_BACK
    for off in (HEADER_LEN_OFF, ns - back):
        struct.pack_into("<I", out, off, struct.unpack_from("<I", out, off)[0] + delta)
    result = bytes(out)
    problems = check_layout(result)
    after = [x for x in records(result) if x["name_off"] == ns]
    if not after or after[0]["kind"] != r["kind"] or after[0]["name"] != new:
        problems.append(f"renamed record not found again at {ns} as {r['kind']} {new!r}")
    if problems:
        raise ValueError(f"patched file fails validation: {problems}")
    return result


def rename_many(data: bytes, changes: list[tuple]) -> bytes:
    """Apply several renames given as (old, new) or (old, new, kind); each re-scans, so shifted offsets never go stale."""
    for change in changes:
        old, new = change[0], change[1]
        kind = change[2] if len(change) > 2 else None
        data = rename(data, old, new, kind)
    return data
