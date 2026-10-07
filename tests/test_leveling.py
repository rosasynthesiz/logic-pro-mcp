"""Pins down the pure planning/maths layer of logic_mcp.leveling: plan().

Every case here builds `stem()` dicts by hand -- the shape plan() actually reads off each stem (silent,
lufs, short, name, role, true_peak_db, correlation) -- rather than going through analyse()/audio.measure(),
so nothing here renders or reads real audio, and Logic never needs to be open.

Two of these tests exist because reading the module surfaced something worth pinning precisely rather than
asserting loosely:

* plan()'s own module docstring says "the reference stem ... is left alone", and that's true of the
  per-stem loop in isolation (its target offset and current offset are both by-construction zero). But the
  later "shift the whole set down" step (leveling.py, the block right after the per-stem loop) applies to
  EVERY move in the list, reference included, whenever another stem's gain survives the headroom clamp
  positive. So the reference's *final* gain_db is only guaranteed to be exactly 0 in scenarios where nothing
  else needs a boost -- see test_reference_gets_no_move_when_nothing_needs_a_boost for that guarantee, and
  test_reference_moves_too_once_the_global_shift_fires for the documented exception. Flagged as a suspected
  discrepancy between the docstring and the code in the final report, not fixed here.
* by that same shift step, a stem's final gain_db can never come back positive -- any positive move gets
  entirely absorbed into pulling every other stem down by the same amount instead (see the comment at that
  code: "so nothing is pushed up into the ceiling"). So "a quiet stem is planned louder" has to be read off
  gain_uncapped_db (the pre-clamp, pre-shift number, which is what actually carries the sign of the raw
  measurement-vs-target gap), not the final gain_db -- see test_quiet_stem_wants_a_boost_loud_stem_wants_a_cut.
"""
from __future__ import annotations

from logic_mcp import leveling


def stem(name: str, lufs: float, *, role: str, true_peak_db: float | None = -6.0,
         crest_db: float | None = 10.0, correlation: float | None = 1.0, silent: bool = False,
         short: str | None = None) -> dict:
    """A hand-built measurement dict carrying exactly the fields plan() reads off a stem."""
    return {
        "name": name, "short": short or name, "lufs": lufs, "true_peak_db": true_peak_db,
        "crest_db": crest_db, "correlation": correlation, "silent": silent, "role": role,
    }


# ---------------------------------------------------------------------------
# a stem already at its target offset gets no move
# ---------------------------------------------------------------------------

def test_stem_already_at_target_offset_gets_a_zero_move():
    # ROLE_TARGETS["bass"] == -2.5 LU relative to the reference; put the bass stem exactly there.
    stems = [
        stem("Lead Vocal.wav", -14.0, role="lead_vocal"),
        stem("Bass DI.wav", -16.5, role="bass"),   # -16.5 - (-14.0) == -2.5, already on target
    ]
    plan = leveling.plan(stems)
    bass = next(m for m in plan["moves"] if m["role"] == "bass")
    assert bass["gain_db"] == 0.0
    assert bass["gain_uncapped_db"] == 0.0
    assert bass["reduced_for_headroom_db"] is None


# ---------------------------------------------------------------------------
# sign of the move: quiet -> boost, loud -> cut
# ---------------------------------------------------------------------------

def test_quiet_stem_wants_a_boost_loud_stem_wants_a_cut():
    stems = [
        stem("Lead Vocal.wav", -14.0, role="lead_vocal"),
        # guitar target is -6.0 LU; sitting at -24.0 LUFS (-10.0 relative) it is 4 dB quieter than target
        stem("Quiet Guitar.wav", -24.0, role="guitar", true_peak_db=-10.0),
        # drums target is -2.0 LU; sitting at -6.0 LUFS (+8.0 relative) it is 10 dB louder than target
        stem("Loud Drums.wav", -6.0, role="drums", true_peak_db=-6.0),
    ]
    plan = leveling.plan(stems)
    guitar = next(m for m in plan["moves"] if m["role"] == "guitar")
    drums = next(m for m in plan["moves"] if m["role"] == "drums")

    # gain_uncapped_db is the pre-clamp, pre-shift number and carries the raw measurement-vs-target sign
    assert guitar["gain_uncapped_db"] == 4.0
    assert drums["gain_uncapped_db"] == -10.0

    # the final, actionable gain_db can never be positive (see module docstring above) but the relative
    # direction survives: the stem that wanted a boost ends up less cut than the one that wanted a cut
    assert guitar["gain_db"] > drums["gain_db"]


# ---------------------------------------------------------------------------
# regression: the reference is not itself given a move, in the scenario that has no boost to absorb
# ---------------------------------------------------------------------------

def test_reference_gets_no_move_when_nothing_needs_a_boost():
    """When every non-reference stem is being cut (never boosted), nothing triggers the global downward
    shift, and the reference's own gain -- by construction 0.0 target offset against itself -- stays
    exactly 0.0. This is the common case (nothing in the mix needs to get louder) and the one the
    regression is about."""
    stems = [
        stem("Lead Vocal.wav", -14.0, role="lead_vocal"),
        # +8.0 LU hotter than the reference against a -2.0 LU target -> wants a big cut, nothing to boost
        stem("Loud Drums.wav", -6.0, role="drums"),
    ]
    plan = leveling.plan(stems)
    ref_move = next(m for m in plan["moves"] if m["role"] == "lead_vocal")
    assert ref_move["gain_db"] == 0.0
    assert ref_move["gain_uncapped_db"] == 0.0
    assert plan["reference"]["name"] == "Lead Vocal.wav" or plan["reference"]["name"] == ref_move["name"]


def test_reference_moves_too_once_the_global_shift_fires():
    """SUSPECTED DISCREPANCY (see module docstring above and the final report): once another stem's boost
    survives the headroom clamp, the "shift the whole set down" step applies to every move including the
    reference's, contradicting the module docstring's "the reference stem ... is left alone". Pinning the
    actual current behaviour rather than the documented one."""
    stems = [
        stem("Lead Vocal.wav", -14.0, role="lead_vocal"),
        stem("Quiet Guitar.wav", -24.0, role="guitar", true_peak_db=-10.0),   # wants +4 dB, headroom allows it
    ]
    plan = leveling.plan(stems)
    ref_move = next(m for m in plan["moves"] if m["role"] == "lead_vocal")
    assert ref_move["gain_db"] == -4.0
    assert ref_move["gain_uncapped_db"] == 0.0   # its own target math still says "don't move"
    assert ref_move["shifted_by_db"] == -4.0


# ---------------------------------------------------------------------------
# gain moves are clamped to max_move_db
# ---------------------------------------------------------------------------

def test_gain_is_clamped_to_max_move_db():
    """max_move_db defaults to 9.0 -- roughly the largest single trim a mixing engineer would trust on a
    stem before deciding the take itself needs re-recording or re-balancing rather than patched with gain.
    A stem 26 dB off target must still be capped there, not handed back as a -26 dB move."""
    stems = [
        stem("Lead Vocal.wav", -14.0, role="lead_vocal"),
        # target -2.0 LU, sitting at +12.0 LU relative -> wants -26 dB after target math; low true peak
        # keeps the headroom guard out of the way so the max_move_db clamp is the only thing in play
        stem("Way Too Loud Drums.wav", 10.0, role="drums", true_peak_db=-20.0),
    ]
    plan = leveling.plan(stems, max_move_db=9.0)
    drums = next(m for m in plan["moves"] if m["role"] == "drums")
    assert drums["gain_uncapped_db"] == -26.0
    assert drums["gain_db"] == -9.0
    assert abs(drums["gain_db"]) <= 9.0


def test_gain_clamp_respects_a_custom_max_move_db():
    stems = [
        stem("Lead Vocal.wav", -14.0, role="lead_vocal"),
        stem("Way Too Loud Drums.wav", 10.0, role="drums", true_peak_db=-20.0),
    ]
    plan = leveling.plan(stems, max_move_db=3.0)
    drums = next(m for m in plan["moves"] if m["role"] == "drums")
    assert drums["gain_db"] == -3.0


# ---------------------------------------------------------------------------
# empty / unmeasurable input must not raise
# ---------------------------------------------------------------------------

def test_plan_with_empty_stem_list_returns_an_error_not_a_crash():
    result = leveling.plan([])
    assert result == {"error": "no measurable stems", "stems": []}


def test_plan_with_only_silent_stems_returns_an_error_not_a_crash():
    stems = [stem("Silence.wav", -70.0, role="other", silent=True)]
    result = leveling.plan(stems)
    assert result["error"] == "no measurable stems"
    assert result["stems"] == stems


def test_summarise_reports_the_plan_error_directly():
    assert leveling.summarise(leveling.plan([])) == "no measurable stems"
