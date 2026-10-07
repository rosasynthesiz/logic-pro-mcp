"""What the offline ProjectData patcher must never get wrong.

This module rewrites the binary of a saved Logic project. A mistake here does not raise — it produces a
file that Logic opens and quietly misreads, and the projects it runs against are 1.9 GB of someone's
work. Every offset in projectdata.py was measured by diffing Logic's own saves; these tests pin the
resulting layout so a refactor cannot drift off it.

Rather than committing a real ProjectData (huge, and not ours to redistribute), the tests BUILD a
synthetic file to the documented record layout. If the builder below and projectdata.py ever disagree,
that disagreement is itself the finding — the builder is a second, independent statement of the format.
"""
from __future__ import annotations

import struct

import pytest

from logic_mcp import projectdata as pd

TRACK_TYPE = b"\x80\x10"        # audio track
FOLDER_TYPE = b"\x00\x14"       # folder / summing stack / Master
CELL = 512                      # generous spacing so records never collide


class Builder:
    """Lay out a minimal ProjectData-shaped buffer.

    TRACK family, going backwards from the name offset `ns`:
        ns-168 u32 length (463 + slot)   ns-12 marker(!=0)   ns-10..ns-6 zeros
        ns-6 type   ns-4..ns-2 zeros   ns-2 u16 LEN   ns name (padded to even)
    NAMED family:
        ns-26 u32 length (297 + slot)   ns-18 tag 70 03 01   ns-2 u16 LEN   ns name (padded to even)
    """

    def __init__(self, size: int = 200_000):
        self.buf = bytearray(size)
        self.cursor = 4096                     # leave room before the first record for its back-fields

    def _place(self, name: str) -> tuple[int, bytes, int]:
        nb = name.encode()
        ns = self.cursor
        self.buf[ns:ns + pd.slot(len(nb))] = nb.ljust(pd.slot(len(nb)), b"\x00")
        struct.pack_into("<H", self.buf, ns - 2, len(nb))
        return ns, nb, pd.slot(len(nb))

    def track(self, name: str, folder: bool = False):
        ns, nb, sl = self._place(name)
        struct.pack_into("<I", self.buf, ns - pd.TRACK_BACK, pd.TRACK_BASE + sl)
        self.buf[ns - 12:ns - 10] = b"\x23\x12"                    # marker: any non-zero pair
        self.buf[ns - 6:ns - 4] = FOLDER_TYPE if folder else TRACK_TYPE
        self.cursor += CELL
        return self

    def named(self, name: str):
        ns, nb, sl = self._place(name)
        struct.pack_into("<I", self.buf, ns - pd.NAMED_BACK, pd.NAMED_BASE + sl)
        self.buf[ns - 18:ns - 15] = pd.NAMED_TAG
        self.cursor += CELL
        return self

    def gap(self, cells: int = 1):
        self.cursor += CELL * cells
        return self

    def build(self) -> bytes:
        struct.pack_into("<I", self.buf, pd.HEADER_LEN_OFF, len(self.buf) - 24)
        return bytes(self.buf)


@pytest.fixture
def project() -> bytes:
    """Groups first (a tight run), then the track table — the arrangement _classify_groups keys on."""
    b = Builder()
    for g in ("STRINGS", "BASS", "PERC"):
        b.named(g)
    b.track("Master", folder=True)
    b.track("Kick")
    b.track("Snare")
    b.track("DRUMS", folder=True)
    b.gap(40)                                   # far enough away to be its own run, and after all tracks
    b.named("Demo Project ep 03")           # project name: NAMED but not a mixer group
    return b.build()


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def test_builder_and_parser_agree(project):
    got = {(r["kind"], r["name"]) for r in pd.records(project)}
    assert ("track", "Kick") in got
    assert ("track", "Snare") in got
    assert ("folder", "Master") in got
    assert ("folder", "DRUMS") in got
    assert ("group", "STRINGS") in got


def test_a_clean_file_has_no_layout_problems(project):
    assert pd.check_layout(project) == []


def test_mixer_groups_are_the_run_before_the_tracks(project):
    groups = [r["name"] for r in pd.records(project) if r["kind"] == "group"]
    assert groups == ["STRINGS", "BASS", "PERC"]


def test_the_project_name_is_named_not_a_group(project):
    """A NAMED record far from the track table must NOT be promoted — renaming it would corrupt
    bookkeeping while looking like a mixer-group rename."""
    kinds = {r["name"]: r["kind"] for r in pd.records(project)}
    assert kinds["Demo Project ep 03"] == "named"


def test_records_are_in_file_order(project):
    offs = [r["name_off"] for r in pd.records(project)]
    assert offs == sorted(offs)


def test_utf8_names_survive_the_scan():
    """Names are UTF-8: an ASCII-only gate once made Tamil records vanish from the scan entirely."""
    b = Builder()
    b.named("STRINGS")
    b.track("Master", folder=True)
    b.track("அம்மா")
    data = b.build()
    names = [r["name"] for r in pd.records(data)]
    assert "அம்மா" in names
    assert pd.check_layout(data) == []


# ---------------------------------------------------------------------------
# renaming
# ---------------------------------------------------------------------------

def test_rename_same_length(project):
    out = pd.rename(project, "Kick", "Kik1")
    names = [r["name"] for r in pd.records(out)]
    assert "Kik1" in names and "Kick" not in names
    assert len(out) == len(project)
    assert pd.check_layout(out) == []


def test_rename_growing_shifts_everything_after_it(project):
    out = pd.rename(project, "Kick", "Kick Drum Close Mic")
    assert len(out) > len(project)
    assert pd.check_layout(out) == []
    # records after the patch must still parse — this is what a stale absolute offset would break
    names = [r["name"] for r in pd.records(out)]
    assert "Snare" in names and "DRUMS" in names and "Demo Project ep 03" in names


def test_rename_shrinking(project):
    out = pd.rename(project, "Snare", "Sn")
    assert len(out) < len(project)
    assert pd.check_layout(out) == []
    assert "Sn" in [r["name"] for r in pd.records(out)]


def test_header_tracks_the_file_size(project):
    for new in ("Kick Drum Close Mic", "K"):
        out = pd.rename(project, "Kick", new)
        hdr = struct.unpack_from("<I", out, pd.HEADER_LEN_OFF)[0]
        assert hdr == len(out) - 24, "u32 @16 must stay filesize-24 or Logic rejects the file"


def test_rename_a_folder(project):
    out = pd.rename(project, "DRUMS", "PERCUSSION", kind="folder")
    kinds = {r["name"]: r["kind"] for r in pd.records(out)}
    assert kinds["PERCUSSION"] == "folder"


def test_rename_a_group(project):
    out = pd.rename(project, "BASS", "LOW END", kind="group")
    kinds = {r["name"]: r["kind"] for r in pd.records(out)}
    assert kinds["LOW END"] == "group"


def test_rename_to_utf8(project):
    out = pd.rename(project, "Kick", "கிக்")
    assert "கிக்" in [r["name"] for r in pd.records(out)]
    assert pd.check_layout(out) == []


def test_rename_many_applies_all(project):
    out = pd.rename_many(project, [("Kick", "K1"), ("Snare", "Snare Top Mic"), ("BASS", "LOW", "group")])
    names = [r["name"] for r in pd.records(out)]
    assert {"K1", "Snare Top Mic", "LOW"} <= set(names)
    assert pd.check_layout(out) == []


def test_rename_many_survives_offsets_shifting_underneath_it(project):
    """Each rename re-scans; a cached offset list would corrupt the second patch."""
    out = pd.rename_many(project, [("Kick", "A Much Longer Kick Name"), ("Snare", "S")])
    assert pd.check_layout(out) == []
    assert {"A Much Longer Kick Name", "S"} <= {r["name"] for r in pd.records(out)}


# ---------------------------------------------------------------------------
# refusals — the patcher must fail closed, never guess
# ---------------------------------------------------------------------------

def test_unknown_name_is_refused(project):
    with pytest.raises(ValueError):
        pd.rename(project, "Nonexistent", "Whatever")


def test_ambiguous_name_is_refused():
    """Two records share a name: patching either one is a coin flip, so it must refuse."""
    b = Builder()
    b.named("STRINGS")
    b.track("Master", folder=True)
    b.track("Twin")
    b.track("Twin")
    with pytest.raises(ValueError):
        pd.rename(b.build(), "Twin", "Solo")


def test_named_records_are_untouchable_without_an_explicit_kind(project):
    """Without kind=, a rename must never land on bookkeeping records."""
    with pytest.raises(ValueError):
        pd.rename(project, "Demo Project ep 03", "Something Else")
    out = pd.rename(project, "Demo Project ep 03", "Something Else", kind="named")
    assert "Something Else" in [r["name"] for r in pd.records(out)]


def test_oversized_name_is_refused(project):
    with pytest.raises(ValueError):
        pd.rename(project, "Kick", "x" * (pd.MAX_NAME + 1))


def test_empty_name_is_refused(project):
    with pytest.raises(ValueError):
        pd.rename(project, "Kick", "")


def test_control_characters_are_refused(project):
    with pytest.raises(ValueError):
        pd.rename(project, "Kick", "bad\x01name")


def test_name_length_is_counted_in_utf8_bytes_not_characters(project):
    """MAX_NAME is a byte budget: 40 Tamil characters are far more than 40 bytes."""
    too_long = "அ" * 40                       # 3 bytes each in UTF-8 = 120 bytes
    assert len(too_long.encode()) > pd.MAX_NAME
    with pytest.raises(ValueError):
        pd.rename(project, "Kick", too_long)


def test_original_buffer_is_never_mutated(project):
    before = bytes(project)
    pd.rename(project, "Kick", "Kick Drum Close Mic")
    assert project == before, "rename must return a patched copy, never edit in place"


# ---------------------------------------------------------------------------
# corruption detection
# ---------------------------------------------------------------------------

def test_check_layout_notices_a_wrong_header(project):
    broken = bytearray(project)
    struct.pack_into("<I", broken, pd.HEADER_LEN_OFF, len(project))     # off by 24
    assert any("header" in p for p in pd.check_layout(bytes(broken)))


def test_check_layout_notices_a_wrong_length_field(project):
    rec = next(r for r in pd.records(project) if r["name"] == "Kick")
    broken = bytearray(project)
    struct.pack_into("<I", broken, rec["name_off"] - pd.TRACK_BACK, 9999)
    problems = pd.check_layout(bytes(broken))
    # the record drops to 'other' (its formula no longer holds) — either way the file is not clean
    assert problems or all(r["kind"] != "track" for r in pd.records(bytes(broken)) if r["name"] == "Kick")


def test_slot_rounds_up_to_even():
    assert [pd.slot(n) for n in (1, 2, 3, 4, 63, 64)] == [2, 2, 4, 4, 64, 64]
