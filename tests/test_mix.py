"""Pins down the pure resolution layer of logic_mcp.mix: lookup() and family_of().

apply() is deliberately out of scope here -- it drives Logic's plugin windows and the OSC surface, and
none of that is reachable with Logic closed and no Accessibility grant.

mix_recipes.json is loaded independently in this file (from the same package path mix.py itself uses,
not a hand-typed copy) so the "every term resolves, no term collides, every by_source key is a real
family" assertions are driven by the actual data on disk, not a list retyped here that could drift from it.

Historical note: mix.py used to carry a SECOND recipe store -- a hand-written 8-entry RECIPES dict read by
parse() and recipe_for(), which nothing in the codebase ever called, and whose term matching had no word
boundary (so "do something" resolved to the "thin" recipe via the substring inside "something"). It was
removed on 2026-09-09; the last test in this file keeps it from coming back.
"""
from __future__ import annotations

import json
import os

import pytest

from logic_mcp import instruments, mix

RECIPES_JSON_PATH = os.path.join(os.path.dirname(mix.__file__), "mix_recipes.json")


@pytest.fixture(scope="module")
def recipe_table() -> list[dict]:
    """The same file mix.py loads at import time, read fresh here rather than trusting a copy-pasted list."""
    with open(RECIPES_JSON_PATH, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# data integrity: every recipe carries what mix.py's apply()/_apply_over_osc actually read off it
# ---------------------------------------------------------------------------

def test_recipe_table_has_34_recipes_and_180_terms(recipe_table):
    # not load-bearing on its own, but a sudden change in these counts means the JSON was edited --
    # worth a human looking at the diff before the rest of this file's assertions are trusted.
    assert len(recipe_table) == 34
    assert sum(len(r["terms"]) for r in recipe_table) == 180


def test_every_recipe_has_the_keys_mix_py_reads(recipe_table):
    # apply() and _apply_over_osc() read rec["terms"], rec["plugin"], rec["settings"], rec["by_source"]
    # and rec["caution"] off every recipe dict (mix.py lines ~140-214). Missing any of these raises a
    # KeyError deep inside a Logic Accessibility call, which is a much worse place to discover a typo in
    # mix_recipes.json than here.
    required = {"terms", "plugin", "settings", "by_source", "caution"}
    for r in recipe_table:
        assert required <= set(r.keys()), f"{r.get('terms')} missing {required - set(r.keys())}"


def test_every_settings_entry_has_parameter_value_and_unit(recipe_table):
    # _apply_over_osc and apply() both do s_["parameter"], s_["value"], s_["unit"] for every settings entry.
    for r in recipe_table:
        for s in r["settings"]:
            assert {"parameter", "value", "unit"} <= set(s.keys()), r["terms"]


def test_every_term_is_non_empty_string(recipe_table):
    for r in recipe_table:
        for t in r["terms"]:
            assert isinstance(t, str) and t.strip() == t and t != ""


# ---------------------------------------------------------------------------
# lookup(): every one of the 180 terms must resolve back to its own recipe
# ---------------------------------------------------------------------------

def test_every_term_resolves_via_lookup_to_its_own_recipe(recipe_table):
    checked = 0
    for recipe in recipe_table:
        for term in recipe["terms"]:
            got = mix.lookup(term)
            assert got == recipe, f"term {term!r} resolved to {got and got['terms']!r}, expected {recipe['terms']!r}"
            checked += 1
    assert checked == 180, "every term in the table should have been exercised, not a subset"


def test_no_term_is_claimed_by_two_recipes(recipe_table):
    """Two recipes sharing a term would make lookup() silently pick one and mask a data-entry bug."""
    owner: dict[str, list[str]] = {}
    for r in recipe_table:
        for t in r["terms"]:
            owner.setdefault(t, []).append(r["terms"][0])
    collisions = {t: owners for t, owners in owner.items() if len(owners) > 1}
    assert collisions == {}


# ---------------------------------------------------------------------------
# lookup(): longest matching term wins, using real overlapping terms from the JSON
# ---------------------------------------------------------------------------

def test_lookup_longest_match_wins_dull_it_down_over_dull():
    """'dull it down' (12 chars, the low-pass recipe) contains 'dull' (4 chars, the dull/brighten
    recipe) as a substring -- the longer, more specific phrase must win."""
    got = mix.lookup("please dull it down a bit")
    assert "dull it down" in got["terms"]
    assert got["terms"][0] == "low-pass it"


def test_lookup_longest_match_wins_no_boom_over_boom():
    """'no boom' (the low-rumble-cut recipe) contains 'boom' (the boomy recipe's own term) as a
    substring -- same longest-wins property, different pair of recipes."""
    got = mix.lookup("there's no boom in the low end")
    assert "no boom" in got["terms"]
    assert got["terms"][0] == "less rumble"


def test_lookup_returns_none_for_unrecognised_command():
    assert mix.lookup("please make it sound like a saxophone solo") is None


def test_lookup_is_case_insensitive():
    got = mix.lookup("MUDDY VOCALS")
    assert got["terms"][0] == "muddy"


# ---------------------------------------------------------------------------
# by_source families must be families instruments.py can actually produce
# ---------------------------------------------------------------------------

def test_by_source_keys_are_real_instrument_families(recipe_table):
    # classify() can return any FAMILIES name, plus "mix" (MIX_PATTERNS) and "unknown" (fallback) --
    # see group_tracks()'s `order` list for the authoritative full set.
    producible = {family for family, _, _ in instruments.FAMILIES} | {"mix", "unknown"}
    for r in recipe_table:
        bad = set(r["by_source"].keys()) - producible
        assert not bad, f"recipe {r['terms'][0]!r} has by_source key(s) {bad} instruments.py never produces"


def test_by_source_families_actually_used_in_the_table(recipe_table):
    """Sanity check the cross-reference itself isn't vacuously true."""
    used = {fam for r in recipe_table for fam in r["by_source"]}
    assert used == {"bass", "drums", "mix", "strings", "vocals"}


# ---------------------------------------------------------------------------
# family_of()
# ---------------------------------------------------------------------------

def test_family_of_matches_a_family_word():
    assert mix.family_of("cut the muddy vocals") == "vocals"


def test_family_of_none_when_no_family_word_present():
    assert mix.family_of("please leave it alone") is None


def test_family_of_requires_a_word_boundary():
    """FAMILY_WORDS is matched with \\b...\\b, so 'vocal' does not match inside 'vocals' -- the dict is
    walked in insertion order and 'vocals' (the later, longer key) is what actually fires here."""
    assert mix.family_of("make the vocals brighter") == "vocals"


# ---------------------------------------------------------------------------
# removed 2026-09-09: parse() and recipe_for()
# ---------------------------------------------------------------------------


def test_the_orphaned_recipe_path_is_gone():
    """parse()/recipe_for() and their hand-written RECIPES dict were dead — nothing in the repo called
    them, and they shadowed the real 34-recipe table with an 8-entry one whose term matching had no word
    boundary ("do something" resolved to the "thin" recipe via the substring in "something").

    Dead code that reads like live code is a trap, so it was deleted. This test keeps it deleted: if any
    of these names comes back, it needs to be wired to RECIPE_TABLE and given word-boundary matching.
    """
    for gone in ('parse', 'recipe_for', 'RECIPES', 'SYNONYMS'):
        assert not hasattr(mix, gone), f'{gone} is back — wire it to RECIPE_TABLE or remove it again'


def test_the_live_resolution_path_survived_the_removal():
    assert mix.lookup('cut the mud in the strings')
    assert mix.family_of('cut the mud in the strings') == 'strings'
    assert len(mix.RECIPE_TABLE) == 34
