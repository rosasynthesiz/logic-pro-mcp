"""The grammar is pure functions with no IO, so there is no excuse for thin coverage here.

`spoken_number` gets hammered across its whole declared range (0-99, see its docstring for why that's
the cap) because number parsing is where these hand-rolled ASR grammars quietly rot: a word out of
range, a decimal digit dropped, a sign word missed, and a fader lands on the wrong dB forever. `parse`
is tested per command family, plus the two properties the whole design leans on — an explicit number
takes the hand out of the loop, and genuine ambiguity refuses rather than guesses.
"""
from __future__ import annotations

import pytest

from logic_mcp.gesture import grammar
from logic_mcp.gesture import protocol as P

# --------------------------------------------------------------------------- spoken_number

_ONES_WORDS = ["zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
               "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen",
               "eighteen", "nineteen"]
_TENS_WORDS = {20: "twenty", 30: "thirty", 40: "forty", 50: "fifty", 60: "sixty", 70: "seventy",
               80: "eighty", 90: "ninety"}


def _word_for(n: int) -> str:
    """Build the English cardinal for 0-99 independently of grammar.py's own tables, so this test
    can't pass just because both sides share the same typo."""
    if n < 20:
        return _ONES_WORDS[n]
    tens, ones = divmod(n, 10)
    word = _TENS_WORDS[tens * 10]
    return f"{word} {_ONES_WORDS[ones]}" if ones else word


@pytest.mark.parametrize("n", range(100))
def test_spoken_number_covers_zero_to_ninety_nine(n):
    assert grammar.spoken_number(_word_for(n)) == pytest.approx(float(n))


@pytest.mark.parametrize("n", [0, 1, 3, 12, 19, 20, 41, 99])
def test_spoken_number_minus_and_negative(n):
    word = _word_for(n)
    assert grammar.spoken_number(f"minus {word}") == pytest.approx(-float(n))
    assert grammar.spoken_number(f"negative {word}") == pytest.approx(-float(n))


def test_spoken_number_minus_three_point_five():
    assert grammar.spoken_number("minus three point five") == pytest.approx(-3.5)


def test_spoken_number_forty_one():
    assert grammar.spoken_number("forty one") == pytest.approx(41.0)


def test_spoken_number_multi_digit_decimal():
    assert grammar.spoken_number("three point one four") == pytest.approx(3.14)


def test_spoken_number_bare_point():
    assert grammar.spoken_number("point five") == pytest.approx(0.5)


def test_spoken_number_hyphenated_compound():
    assert grammar.spoken_number("twenty-three") == pytest.approx(23.0)


def test_spoken_number_literal_digits():
    assert grammar.spoken_number("41") == pytest.approx(41.0)
    assert grammar.spoken_number("-3") == pytest.approx(-3.0)
    assert grammar.spoken_number("3.5") == pytest.approx(3.5)
    assert grammar.spoken_number("-3.5") == pytest.approx(-3.5)


@pytest.mark.parametrize("text", [None, "", "  ", "banana", "one twenty", "minus", "point"])
def test_spoken_number_rejects_garbage(text):
    """'one twenty' is the important one here: word form is capped at ninety-nine, so this must refuse
    rather than silently return 1.0 off the leading 'one'."""
    assert grammar.spoken_number(text) is None


# --------------------------------------------------------------------------- axis bindings (hand takes over)

@pytest.mark.parametrize("cmd,verb", [
    ("volume on strings", "volume"),
    ("pan on strings", "pan"),
    ("param on strings", "param"),
    ("ride strings", "volume"),         # "ride" has always meant riding the volume fader
])
def test_axis_bind_leaves_value_none(cmd, verb):
    intent = grammar.parse(cmd)
    assert intent is not None
    assert intent.verb == verb
    assert intent.target == "strings"
    assert intent.value is None


# --------------------------------------------------------------------------- explicit values (hand NOT involved)

def test_volume_on_binds_hand_but_explicit_value_does_not():
    bound = grammar.parse("volume on strings")
    assert bound.value is None

    explicit = grammar.parse("volume strings to minus three")
    assert explicit.verb == "volume"
    assert explicit.target == "strings"
    assert explicit.value == pytest.approx(-3.0)
    assert explicit.unit == "dB"


def test_set_to_explicit_db():
    intent = grammar.parse("set strings to -3 dB")
    assert intent.verb == "volume"
    assert intent.target == "strings"
    assert intent.value == pytest.approx(-3.0)
    assert intent.unit == "dB"


def test_set_to_without_unit_is_ambiguous():
    """No 'volume' keyword and no unit word — could mean dB, pan, or something else. Refuse rather
    than guess, per this repo's fail-closed convention (see tests/test_osc.py's own house style)."""
    assert grammar.parse("set strings to 3") is None


def test_pan_left_with_magnitude():
    intent = grammar.parse("pan strings left twelve")
    assert intent.verb == "pan"
    assert intent.target == "strings"
    assert intent.value == pytest.approx(-12.0)
    assert intent.unit == "pan"


def test_pan_hard_right():
    intent = grammar.parse("pan strings hard right")
    assert intent.value == pytest.approx(100.0)
    assert intent.unit == "pan"


def test_pan_bare_direction_without_magnitude_is_none():
    assert grammar.parse("pan strings left") is None


def test_down_two_db_is_relative_and_explicit():
    intent = grammar.parse("strings down two dB")
    assert intent.verb == "volume_down"
    assert intent.target == "strings"
    assert intent.value == pytest.approx(2.0)
    assert intent.unit == "dB"


def test_up_without_unit_is_ambiguous():
    assert grammar.parse("strings up two") is None


# --------------------------------------------------------------------------- track ops

@pytest.mark.parametrize("verb", ["select", "mute", "unmute", "solo", "unsolo", "arm", "disarm", "rename"])
def test_track_ops_parse(verb):
    intent = grammar.parse(f"{verb} strings")
    assert intent is not None
    assert intent.verb == verb
    assert intent.target == "strings"


def test_track_op_without_target_is_none():
    assert grammar.parse("mute") is None


# --------------------------------------------------------------------------- transport

@pytest.mark.parametrize("cmd,verb", [
    ("play", "play"),
    ("stop", "stop"),
    ("record", "record"),
    ("go to start", "goto_start"),
    ("cycle on", "cycle_on"),
    ("cycle off", "cycle_off"),
    ("metronome on", "metronome_on"),
    ("metronome off", "metronome_off"),
])
def test_transport_exact_commands(cmd, verb):
    intent = grammar.parse(cmd)
    assert intent is not None
    assert intent.verb == verb
    assert intent.target is None
    assert intent.value is None


def test_go_to_bar_with_spoken_number():
    intent = grammar.parse("go to bar forty one")
    assert intent.verb == "goto_bar"
    assert intent.value == pytest.approx(41.0)


def test_tempo_with_literal_digits():
    intent = grammar.parse("tempo 120")
    assert intent.verb == "tempo"
    assert intent.value == pytest.approx(120.0)


# --------------------------------------------------------------------------- plugins

def test_plugin_on_track():
    intent = grammar.parse("compressor on bass")
    assert intent.verb == "plugin_add"
    assert intent.target == "bass"
    assert intent.axis == "compressor"


def test_bypass_plugin_on_track():
    intent = grammar.parse("bypass compressor on bass")
    assert intent.verb == "plugin_bypass"
    assert intent.target == "bass"
    assert intent.axis == "compressor"


def test_multi_word_plugin_name_survives():
    intent = grammar.parse("channel eq on drums")
    assert intent.axis == "channel eq"
    assert intent.target == "drums"


# --------------------------------------------------------------------------- project / destructive

@pytest.mark.parametrize("cmd,verb", [
    ("save", "save"),
    ("undo", "undo"),
    ("redo", "redo"),
    ("bounce", "bounce"),
    ("new audio track", "new_audio_track"),
    ("new instrument track", "new_instrument_track"),
])
def test_project_commands(cmd, verb):
    intent = grammar.parse(cmd)
    assert intent is not None
    assert intent.verb == verb


def test_delete_track_parses():
    intent = grammar.parse("delete track strings")
    assert intent.verb == "delete_track"
    assert intent.target == "strings"


def test_destructive_set_contents():
    """The exact membership matters: a gesture must never be able to reach any of these (see
    arbiter.py's rule 3 and GESTURE_VOICE_PLAN.md section 6 on never binding destructive commands to
    a pose)."""
    assert grammar.DESTRUCTIVE == frozenset({"delete_track", "bounce", "save"})


def test_destructive_verbs_are_all_reachable_by_parse():
    """A verb sitting in DESTRUCTIVE that the grammar can never actually produce would be a false sense
    of safety, not real safety."""
    produced = {grammar.parse("save").verb, grammar.parse("bounce").verb,
                grammar.parse("delete track strings").verb}
    assert produced == set(grammar.DESTRUCTIVE)


# --------------------------------------------------------------------------- unknown / ambiguous text

@pytest.mark.parametrize("text", [
    "make the chorus louder than the verse",
    "what time is it",
    "",
    "   ",
    "banana",
])
def test_unknown_text_returns_none(text):
    assert grammar.parse(text) is None


# --------------------------------------------------------------------------- ASR noise normalisation

def test_leading_filler_words_are_stripped():
    assert grammar.parse("uh mute strings").verb == "mute"
    assert grammar.parse("um mute strings").verb == "mute"


def test_logic_leading_filler_is_stripped():
    """Talking *to* Logic ('logic, play') is the single most likely filler this system will hear,
    since the whole point of push-to-talk is that the user is addressing the app."""
    assert grammar.parse("logic play").verb == "play"


def test_trailing_punctuation_is_ignored():
    assert grammar.parse("play.").verb == "play"
    assert grammar.parse("stop!").verb == "stop"


def test_solo_vs_solar_mishearing():
    intent = grammar.parse("solar strings")
    assert intent.verb == "solo"
    assert intent.target == "strings"


def test_arm_vs_are_am_mishearing():
    intent = grammar.parse("are am strings")
    assert intent.verb == "arm"
    assert intent.target == "strings"


def test_bar_vs_far_mishearing():
    intent = grammar.parse("go to far ten")
    assert intent.verb == "goto_bar"
    assert intent.value == pytest.approx(10.0)


# --------------------------------------------------------------------------- track names pass through verbatim

def test_track_name_is_lowercased_and_stripped_but_otherwise_untouched():
    intent = grammar.parse("MUTE   Lead Vocals  ")
    assert intent.verb == "mute"
    assert intent.target == "lead vocals"


def test_multi_word_track_name_survives():
    intent = grammar.parse("solo electric guitar")
    assert intent.target == "electric guitar"


# --------------------------------------------------------------------------- sanity on the return type

def test_parse_returns_an_intent_instance():
    intent = grammar.parse("play")
    assert isinstance(intent, P.Intent)
    assert intent.source == "grammar"
