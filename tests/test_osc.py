"""The OSC wire format and address maths — the parts that can be checked without Logic.

Nothing here opens a socket or advertises Bonjour. That matters beyond convenience: killing or restarting
the advert while Logic has the device bound segfaults Logic, so the surface is never touched from a test.
What is tested is everything that can be got wrong silently — packet framing, and the three address
quirks that were each measured the hard way (reversed insert columns, the off-by-one EQ gain index, and
the two irregular low-band Q names).

Address-quirk expectations are written as literals rather than derived from the code, so that a "cleanup"
of the formula fails here instead of in someone's mix.
"""
from __future__ import annotations

import struct

import pytest

from logic_mcp.channels import osc

# eq_address/insert_address only read module-level tables, never instance state, so they can be called
# off the class with a None self — instantiating LogicPad would open a UDP socket.
eq_address = lambda band, param: osc.LogicPad.eq_address(None, band, param)          # noqa: E731
insert_address = osc.LogicPad.insert_address


# ---------------------------------------------------------------------------
# OSC 1.0 wire format
# ---------------------------------------------------------------------------

def test_encode_decode_round_trip():
    for args in ([], [1.0], [3], ["hello"], [1, 2.5, "x"], [True], [False]):
        addr, got = osc.decode(osc.encode("/1/play", *args))[0]
        assert addr == "/1/play"
        assert len(got) == len(args)
        for a, b in zip(args, got):
            assert b == pytest.approx(a) if isinstance(a, float) else b == a


def test_everything_is_four_byte_aligned():
    """Any misalignment desyncs every later argument, so the length is the whole contract."""
    for args in ([], ["a"], ["abcd"], ["abcde"], [1, "abcd", 2.0], [b"\x01\x02\x03\x04"], [b"\x01"]):
        assert len(osc.encode("/some/address", *args)) % 4 == 0


def test_string_always_gets_a_terminator():
    """A 4-byte string still needs a whole padding word — without it the reader runs off the end."""
    packet = osc.encode("/x", "abcd")
    assert packet.endswith(b"abcd\x00\x00\x00\x00")


def test_blob_padding_has_no_spurious_word():
    """Blobs align but do NOT get a terminator. Reusing the string padder adds a whole word to an
    already-aligned blob and desyncs every argument after it — a real bug this pins shut."""
    addr, args = osc.decode(osc.encode("/x", b"\x01\x02\x03\x04", 7))[0]
    assert args[0] == b"\x01\x02\x03\x04"
    assert args[1] == 7, "the integer after an aligned blob must still decode"


def test_blob_of_awkward_length_round_trips():
    for n in (1, 2, 3, 5, 8, 9):
        _, args = osc.decode(osc.encode("/x", bytes(range(n)), 42))[0]
        assert args[0] == bytes(range(n)) and args[1] == 42


def test_bundles_are_flattened():
    a, b = osc.encode("/1/play", 1.0), osc.encode("/1/stop", 0.0)
    bundle = b"#bundle\x00" + b"\x00" * 8 + struct.pack(">i", len(a)) + a + struct.pack(">i", len(b)) + b
    assert [addr for addr, _ in osc.decode(bundle)] == ["/1/play", "/1/stop"]


def test_address_with_no_type_tag_decodes():
    """A bare page switch ("/3") carries no type-tag string at all."""
    assert osc.decode(osc.encode("/3"))[0][0] == "/3"


def test_unknown_type_tag_raises_rather_than_misparsing():
    """An unknown tag has unknown width, so the cursor is meaningless — every later argument would be
    silently wrong. Refusing is the honest answer."""
    packet = osc._pad(b"/x") + osc._pad(b",Z") + b"\x00\x00\x00\x00"
    with pytest.raises(ValueError):
        osc.decode(packet)


def test_zero_width_tags_do_not_advance_the_cursor():
    packet = osc._pad(b"/x") + osc._pad(b",Ti") + struct.pack(">i", 9)
    _, args = osc.decode(packet)[0]
    assert args[0] is True and args[1] == 9


def test_unsupported_argument_type_is_refused():
    with pytest.raises(TypeError):
        osc.encode("/x", {"not": "encodable"})


def test_bool_is_checked_before_int():
    """bool is a subclass of int; ordering the isinstance chain wrongly sends True as the integer 1."""
    assert osc.decode(osc.encode("/x", True))[0][1] == [True]
    assert osc.decode(osc.encode("/x", 1))[0][1] == [1]


# ---------------------------------------------------------------------------
# measured address quirks
# ---------------------------------------------------------------------------

def test_insert_slots_are_reversed():
    """Measured: insert slot 1 echoes as /3/insertbypass/12/1 — the grid column is 13 - slot."""
    assert insert_address(1) == "/3/insertbypass/12/1"
    assert insert_address(12) == "/3/insertbypass/1/1"
    assert insert_address(1, "edit") == "/3/mInsertEdit/12/1"


@pytest.mark.parametrize("slot", range(1, 13))
def test_insert_columns_are_a_permutation_of_1_to_12(slot):
    col = int(insert_address(slot).split("/")[3])
    assert 1 <= col <= 12


def test_eq_gain_index_is_off_by_one():
    """Measured: /4/gain/3 drives band 4. Losing this shifts every EQ move onto the wrong band."""
    assert eq_address(4, "gain")[0] == "/4/gain/3"
    assert eq_address(2, "gain")[0] == "/4/gain/1"
    assert eq_address(3, "gain")[1] == "/4/logainval"


@pytest.mark.parametrize("band", [1, 8])
def test_filter_bands_have_no_gain(band):
    """Bands 1 and 8 are the high-pass and low-pass filters — asking for gain must refuse, not
    silently drive something else."""
    with pytest.raises(ValueError):
        eq_address(band, "gain")


@pytest.mark.parametrize("band", [1, 8])
def test_filter_bands_do_have_slope(band):
    ctrl, _ = eq_address(band, "slope")
    assert ctrl.endswith("slp")


def test_slope_is_refused_on_non_filter_bands():
    with pytest.raises(ValueError):
        eq_address(4, "slope")


def test_low_band_q_uses_the_irregular_names():
    """Band 3 breaks the <band>q convention used by every other band: lowq / loqval, not loq / loqval."""
    ctrl, disp = eq_address(3, "q")
    assert (ctrl, disp) == ("/4/lowq", "/4/loqval")


def test_other_bands_follow_the_regular_q_convention():
    assert eq_address(4, "q") == ("/4/lomidq", "/4/lomidqval")
    assert eq_address(5, "q") == ("/4/himidq", "/4/himidqval")


def test_all_eight_bands_have_freq_and_q():
    for band in range(1, 9):
        assert eq_address(band, "freq")[0].startswith("/4/")
        assert eq_address(band, "q")[0].startswith("/4/")


def test_unknown_eq_param_is_refused():
    with pytest.raises(ValueError):
        eq_address(4, "resonance")


# ---------------------------------------------------------------------------
# parameter-name resolution (Logic abbreviates everything on the surface)
# ---------------------------------------------------------------------------

def params(*names):
    return [{"name": n, "n": i + 1} for i, n in enumerate(names)]


def test_exact_name_wins_over_alias_and_substring():
    p = params("Gain", "Make Up Gain")
    assert osc.resolve_param("Gain", p)["name"] == "Gain"


def test_logic_abbreviations_resolve():
    assert osc.resolve_param("Threshold", params("Thrs", "Ratio"))["name"] == "Thrs"
    assert osc.resolve_param("Output Level", params("OutLevel"))["name"] == "OutLevel"


def test_deesser_irregular_names_resolve():
    """The DeEsser calls Frequency "DetctrF" and Range "MaxRedct" — recipes are written in full words."""
    assert osc.resolve_param("Frequency", params("DetctrF"))["name"] == "DetctrF"
    assert osc.resolve_param("Range", params("MaxRedct"))["name"] == "MaxRedct"


def test_resolution_is_case_and_space_insensitive():
    assert osc.resolve_param("  threshold  ", params("Thrs"))["name"] == "Thrs"


def test_substring_fallback():
    assert osc.resolve_param("Attack", params("Attack Time"))["name"] == "Attack Time"


def test_unknown_parameter_returns_none():
    assert osc.resolve_param("Warp Drive", params("Gain", "Ratio")) is None


def test_empty_parameter_list_returns_none():
    assert osc.resolve_param("Gain", []) is None


def test_band_params_expand_across_bands():
    p = params("Ba1CompT", "Ba2CompT", "Ba3CompT", "Ba4CompT", "Gain")
    got = osc.resolve_band_params("Threshold", p)
    assert [x["name"] for x in got] == ["Ba1CompT", "Ba2CompT", "Ba3CompT", "Ba4CompT"]


def test_band_params_are_returned_in_band_order():
    p = params("Ba3CompR", "Ba1CompR", "Ba2CompR")
    got = osc.resolve_band_params("Ratio", p)
    assert [x["n"] for x in got] == sorted(x["n"] for x in got)


def test_band_params_empty_for_a_single_band_plugin():
    """A generic name only means something multiband when several bands answer to it."""
    assert osc.resolve_band_params("Threshold", params("Thrs", "Ratio")) == []


# ---------------------------------------------------------------------------
# display-string parsing — unit-aware, because plug-ins switch units as values grow
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text,want", [
    ("300 Hz", 300.0),
    ("1.20k", 1200.0),
    ("1.2 kHz", 1200.0),
    ("-3.0 dB", -3.0),
    ("2.500sec", 2500.0),          # FabFilter Pro-C 2 shows seconds where the recipe asks for ms
    ("250 ms", 250.0),             # must NOT be multiplied — "ms" is not "s"
    ("4.0:1", 4.0),
    ("0", 0.0),
])
def test_display_numbers_are_normalised(text, want):
    assert osc._num(text) == pytest.approx(want)


def test_unparseable_display_returns_none():
    for text in (None, "", "---", "n/a"):
        assert osc._num(text) is None


def test_milliseconds_are_not_treated_as_seconds():
    """The seconds rule must never match "ms" — that would make 250 ms read as 250 000 ms."""
    assert osc._num("250 ms") < osc._num("2.5 sec")


def test_connected_is_false_once_the_advertised_address_leaves_this_mac(monkeypatch):
    """2026-10-06: a Wi-Fi lease change left Logic bound to an address this Mac no longer had. `lsof`
    still showed Logic's socket to it, so `connected` stayed true while every send was dropped."""
    import time
    from logic_mcp.channels import osc
    pad = osc.LogicPad.__new__(osc.LogicPad)     # no socket, no dns-sd: only what connected() reads
    pad.ip, pad.port, pad.last_rx = "10.179.131.116", 9000, time.time()
    monkeypatch.setattr(osc, "local_ips", lambda: frozenset({"10.213.26.157"}))
    assert pad.ip_stale is True
    assert pad.connected is False
    monkeypatch.setattr(osc, "local_ips", lambda: frozenset({"10.179.131.116"}))
    assert pad.ip_stale is False
    assert pad.connected is True                 # recent traffic and the address is still ours


# ---------------------------------------------------------------------------
# Only Logic on this Mac may feed the echo cache
# ---------------------------------------------------------------------------

def _bare_pad(ip="192.168.1.31"):
    import threading
    from collections import deque
    pad = osc.LogicPad.__new__(osc.LogicPad)     # no socket, no dns-sd: only what _ingest touches
    pad.ip, pad.port = ip, 9000
    pad.state, pad.log, pad._lock = {}, deque(maxlen=50), threading.Lock()
    pad.rx_count = pad.decode_errors = pad.rejected = 0
    pad.last_rx = 0.0
    return pad


def test_feedback_from_another_host_is_rejected_not_cached():
    """Every `confirmed` this channel reports is read back from the cache, so a datagram from any other
    machine on the Wi-Fi could forge one. The 2026-09-11 eval flagged it; this pins the fix."""
    pad = _bare_pad()
    packet = osc.encode("/1/volume3", 0.75)
    assert pad._ingest(packet, ("10.0.0.99", 9000)) is False
    assert pad.state == {} and pad.rx_count == 0 and pad.rejected == 1
    assert pad._ingest(packet, ("192.168.1.31", 54101)) is True       # Logic, from our own address
    assert pad.state["/1/volume3"] == [0.75] and pad.rx_count == 1
    assert pad._ingest(packet, ("127.0.0.1", 1)) is True              # loopback stays allowed
    assert pad.rejected == 1
