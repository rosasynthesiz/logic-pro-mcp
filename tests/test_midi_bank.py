"""`MidiBus.bank_offset`: which track sits on MCU strip 1, read from the LCD.

Writes over MCU go to a strip, and the strip-to-track mapping is only as good as this function. The
2026-09-11 eval found it answering 0 (should be 2), 0 (should be 8) and 11 (should be None), after which
mute/solo/arm toggled a different track and reported `confirmed` for the one asked. Zero tests then."""
from __future__ import annotations

from logic_mcp.channels.midi import MidiBus


class _Lcd:
    def __init__(self, cells):
        self.lcd = "".join(c.ljust(7) for c in cells) + "\n0      0"


def _bus(cells):
    b = MidiBus.__new__(MidiBus)
    b.mcu = _Lcd(cells)
    return b


TRACKS = [f"Audio {i}" for i in range(1, 25)] + ["Drums", "Bass", "GTR", "Other"]       # 28 tracks


def test_full_bank_in_the_middle_of_the_list():
    b = _bus(["Audo17", "Audo18", "Audo19", "Audo20", "Audo21", "Audo22", "Audo23", "Audo24"])
    assert b.bank_offset(TRACKS) == 16


def test_tail_bank_needs_the_blank_cells_to_line_up():
    # 28 tracks, bank starting at track 25: four names, four blanks.
    b = _bus(["Drums", "Bass", "GTR", "Other", "", "", "", ""])
    assert b.bank_offset(TRACKS) == 24
    # The same four names with the LCD still showing four MORE names is not that bank.
    b = _bus(["Drums", "Bass", "GTR", "Other", "Audo1", "Audo2", "Audo3", "Audo4"])
    assert b.bank_offset(TRACKS) is None


def test_short_window_cannot_claim_a_full_lcd():
    # The eval's "11 (should be None)": an LCD full of names, a list whose tail has fewer tracks than cells.
    b = _bus(["Kick", "Snare", "Hat", "Tom", "Ride", "Crash", "Bass", "Keys"])
    assert b.bank_offset(["Vox", "Gtr", "Pad", "Kick", "Snare", "Hat"]) is None


def test_repeated_names_are_ambiguous_not_a_guess():
    block = ["Kick", "Snare", "Hat", "Tom", "Ride", "Crash", "Bass", "Keys"]
    b = _bus(block)
    assert b.bank_offset(block + block) is None          # offsets 0 and 8 both fit -> refuse
    assert b.bank_offset(block + ["Vox"]) == 0           # one fit -> answer


def test_blank_lcd_is_unknown_and_strip_for_fails_closed():
    b = _bus([""] * 8)
    assert b.bank_offset(TRACKS) is None
    assert b.strip_for(3, TRACKS) is None
    b = _bus(["Audo17", "Audo18", "Audo19", "Audo20", "Audo21", "Audo22", "Audo23", "Audo24"])
    assert b.strip_for(19, TRACKS) == 2                   # track 19 is the third strip of the bank at 16
    assert b.strip_for(3, TRACKS) is None                 # not on the surface right now
