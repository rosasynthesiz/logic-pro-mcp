"""Pins down logic_mcp.instruments: classifying tracks by instrument from their names alone.

The whole reason this module is tricky is that sample-library filenames prefix a vendor/publisher code
that can read exactly like an instrument or section name -- "VOX_LCH_128_bass_synth_something" is a BASS
patch from a library whose vendor code happens to be "VOX", not a vocal. Confusing the two silently
recolours and renames the wrong tracks. These tests cover every family in FAMILIES, then hammer on that
one trap from three different families (vocals/bass, drums/brass, drums/guitar) plus a genuine vocal name
that must still come through as vocals so the anti-false-positive fix hasn't overcorrected the other way.
"""
from __future__ import annotations

from logic_mcp import instruments


# ---------------------------------------------------------------------------
# classify() -- one passing case per family declared in FAMILIES
# ---------------------------------------------------------------------------

def test_classify_percussion_vocal_percussion_entry():
    # "vocal percussion" is caught by its own pattern, listed before the vocals family precisely so it
    # doesn't fall into vocals.
    assert instruments.classify("Vocal Percussion Loop") == ("percussion", "orange")


def test_classify_percussion_general_entry():
    assert instruments.classify("Conga Loop") == ("percussion", "orange")


def test_classify_vocals():
    assert instruments.classify("Lead Vocal") == ("vocals", "blue")


def test_classify_drums():
    assert instruments.classify("Drum Loop") == ("drums", "dark red")


def test_classify_bass():
    assert instruments.classify("Deep Bass") == ("bass", "indigo")


def test_classify_guitar():
    assert instruments.classify("Acoustic Guitar") == ("guitar", "green")


def test_classify_strings():
    assert instruments.classify("String Ensemble") == ("strings", "violet")


def test_classify_woodwind():
    assert instruments.classify("Flute Melody") == ("woodwind", "teal")


def test_classify_brass():
    assert instruments.classify("Trumpet Fanfare") == ("brass", "amber")


def test_classify_keys():
    assert instruments.classify("Rhodes Piano") == ("keys", "azure")


def test_classify_synth():
    assert instruments.classify("Synth Lead") == ("synth", "magenta")


def test_classify_fx():
    assert instruments.classify("Riser FX") == ("fx", "grape")


def test_every_family_in_FAMILIES_is_exercised_above():
    """Guard the test suite itself: if a family is ever added to FAMILIES, this fails until it gets a case."""
    covered = {"percussion", "vocals", "drums", "bass", "guitar", "strings", "woodwind", "brass", "keys",
               "synth", "fx"}
    declared = {family for family, _, _ in instruments.FAMILIES}
    assert declared == covered


def test_classify_mix_pattern():
    assert instruments.classify("Full Mix Bounce") == ("mix", "darkest blue")


def test_classify_unknown_for_hash_named_import():
    assert instruments.classify("abcdef0123456789abcdef01") == ("unknown", None)


def test_classify_unknown_for_generic_track_name():
    assert instruments.classify("Audio 3") == ("unknown", None)


# ---------------------------------------------------------------------------
# the documented trap: vendor/library prefixes that read like something else
# ---------------------------------------------------------------------------

def test_vendor_prefix_vox_is_a_publisher_code_not_vocals():
    """The real example from this project: VOX here is AU/LCH-style vendor code, and the patch is a bass
    synth. Without stripping the prefix, "\\bvox\\b" would fire and mis-tag a bass track as vocals."""
    assert instruments.classify("VOX_LCH_128_bass_synth_something") == ("bass", "indigo")


def test_vendor_prefix_hat_is_not_a_drum_hat():
    """HAT here is a made-up vendor code (like the real VOX one), not a hi-hat -- the drums family's
    "\\bhat\\b" pattern would fire first (drums is checked before brass) if the prefix weren't stripped,
    mis-tagging a trumpet patch as drums."""
    assert instruments.classify("HAT_MXR_115_trumpet_fanfare_line") == ("brass", "amber")


def test_vendor_prefix_kit_is_not_a_drum_kit():
    """KIT here is a vendor code, not "drum kit" -- the drums family's "\\bkit\\b" pattern (checked
    before guitar) would fire if the prefix weren't stripped, mis-tagging a guitar patch as drums."""
    assert instruments.classify("KIT_KLN_090_acoustic_guitar_strum") == ("guitar", "green")


def test_genuine_vocal_name_still_classifies_as_vocals():
    """The fix for the VOX trap must not overcorrect into missing real vocal tracks that carry no
    library-style vendor prefix at all."""
    assert instruments.classify("Lead Vocal")[0] == "vocals"
    assert instruments.classify("Backing Vocals Take 2")[0] == "vocals"


# ---------------------------------------------------------------------------
# group_tracks()
# ---------------------------------------------------------------------------

def test_group_tracks_buckets_by_family_and_tags_each_track():
    tracks = [
        {"index": 1, "name": "Kick Drum"},
        {"index": 2, "name": "Lead Vocal"},
        {"index": 3, "name": "Deep Bass"},
    ]
    grouped = instruments.group_tracks(tracks)

    # declaration order in group_tracks' `order` list is vocals, drums, ..., bass -- NOT input order
    assert list(grouped.keys()) == ["vocals", "drums", "bass"]
    assert grouped["vocals"] == [{"index": 2, "name": "Lead Vocal", "family": "vocals", "colour": "blue"}]
    assert grouped["drums"] == [{"index": 1, "name": "Kick Drum", "family": "drums", "colour": "dark red"}]
    assert grouped["bass"] == [{"index": 3, "name": "Deep Bass", "family": "bass", "colour": "indigo"}]


def test_group_tracks_omits_empty_families():
    tracks = [{"index": 1, "name": "Deep Bass"}]
    grouped = instruments.group_tracks(tracks)
    assert list(grouped.keys()) == ["bass"]
    assert "vocals" not in grouped
    assert "unknown" not in grouped


def test_group_tracks_preserves_track_order_within_a_family():
    tracks = [
        {"index": 1, "name": "Kick Drum"},
        {"index": 2, "name": "Snare Drum"},
        {"index": 3, "name": "Tom Fill"},
    ]
    grouped = instruments.group_tracks(tracks)
    assert [t["index"] for t in grouped["drums"]] == [1, 2, 3]


def test_group_tracks_empty_input():
    assert instruments.group_tracks([]) == {}


# ---------------------------------------------------------------------------
# hint()
# ---------------------------------------------------------------------------

def test_hint_picks_the_single_matching_word():
    assert instruments.hint("Kick Drum Loop") == "Kick"


def test_hint_prefers_longest_and_keeps_two_short_words():
    """"shaker" (6 chars) and "conga" (5 chars) both match; longest first, and since the longest is
    <=6 chars the cap allows a second word."""
    assert instruments.hint("Conga Shaker Loop") == "Shaker Conga"


def test_hint_caps_to_one_word_once_the_longest_exceeds_six_chars():
    """"trombone" (8 chars) beats "kick" (4 chars) on length, and because it's >6 chars the second word
    is dropped so the descriptor stays short."""
    assert instruments.hint("Trombone Kick Hit") == "Trombone"


def test_hint_returns_empty_string_when_nothing_matches():
    assert instruments.hint("Loop One") == ""


def test_hint_is_family_agnostic():
    """hint() searches the full HINTS list and takes no family.

    It used to accept a `family` argument that the body never read, which implied a family-awareness it
    has never had; the parameter was removed on 2026-09-09. If hints ever should depend on the family,
    that is a deliberate change with a signature to match -- not a silently ignored argument.
    """
    import inspect
    assert list(inspect.signature(instruments.hint).parameters) == ["name"]
    # the vendor prefix is stripped by _norm before HINTS matching, same as in classify()
    assert instruments.hint("VOX_LCH_128_bass_synth_something") == ""


# ---------------------------------------------------------------------------
# rename_plan()
# ---------------------------------------------------------------------------

def test_rename_plan_numbers_per_family_in_track_order():
    tracks = [
        {"index": 1, "name": "Kick Drum"},
        {"index": 2, "name": "Snare Drum"},
        {"index": 3, "name": "Choir"},
        {"index": 4, "name": "VOX_LCH_128_bass_synth_something"},
    ]
    plan = instruments.rename_plan(tracks)

    assert plan == [
        {"index": 1, "old": "Kick Drum", "new": "Drums 01 Kick", "family": "drums", "colour": "dark red"},
        {"index": 2, "old": "Snare Drum", "new": "Drums 02 Snare", "family": "drums", "colour": "dark red"},
        {"index": 3, "old": "Choir", "new": "Vox 01 Choir", "family": "vocals", "colour": "blue"},
        # the vendor prefix is stripped for classification+hinting but "old" keeps the untouched name
        {"index": 4, "old": "VOX_LCH_128_bass_synth_something", "new": "Bass 01", "family": "bass",
         "colour": "indigo"},
    ]


def test_rename_plan_new_names_are_capped_at_forty_chars():
    tracks = [{"index": 1, "name": "Glockenspiel Vibraphone Marimba Accordion Harmonium"}]
    plan = instruments.rename_plan(tracks)
    assert len(plan[0]["new"]) <= 40


def test_rename_plan_unknown_family_gets_no_hint_suffix():
    tracks = [{"index": 1, "name": "Audio 3"}]
    plan = instruments.rename_plan(tracks)
    assert plan[0] == {"index": 1, "old": "Audio 3", "new": "Others 01", "family": "unknown", "colour": None}
