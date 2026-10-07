"""The fast path: text straight to `P.Intent`, no model anywhere near it.

This exists because a fader ride can tolerate ~50 ms but a voice command through an LLM costs 1-3 s
(see GESTURE_VOICE_PLAN.md section 2). Most of a mixing session is the same ~40 commands, so a
deterministic parser handles those in well under a millisecond and only the long tail — "make the
chorus louder than the verse" — has to pay for the model.

`parse()` returns `None` whenever it isn't sure, on purpose. That includes plain non-matches, but also
genuine ambiguity: "set strings to 3" could mean dB, pan, or something else, so it refuses rather than
guessing which. Two rules downstream depend on that refusal being conservative:

1. **The grammar never resolves a track name.** "strings" is passed through exactly as heard, lowercased
   and stripped of surrounding whitespace — never fuzzy-matched, never abbreviated, never guessed. Turning
   a word into a track index needs Logic's live track list, which lives far above this module.
2. **An explicit number kills the hand.** `Intent.value` is only ever non-None when the human said a
   number out loud. `gesture/arbiter.py` treats that as "the value is already decided" and does not let a
   hand movement touch it — see `AXIS_VERBS` and the comment on `on_intent`.
"""
from __future__ import annotations

import re

from . import protocol as P

# --------------------------------------------------------------------------- spoken numbers

# English cardinals 0-19, and the multiples of ten from 20-90. Deliberately capped at ninety-nine
# (see `spoken_number`'s docstring) — a DAW's spoken vocabulary rarely needs a word for "one hundred and
# twenty", and typed/ASR-literal digits ("120") already cover that case via the fast path below.
_ONES = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
}
_TENS = {
    "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90,
}


def spoken_number(text: str | None) -> float | None:
    """Parse a number that may be spoken ("minus three point five", "forty one") or already a literal
    ("-3.5", "41"). ASR backends do both depending on the model and the moment, so both must work.

    Word form covers 0-99 ("twenty three" or the ASR-typical hyphenated "twenty-three"), an optional
    leading "minus"/"negative", and an optional "point" followed by one digit-word (or literal digit)
    per decimal place ("three point one four" -> 3.14). Anything left over after that — a stray word,
    a number word out of range ("one twenty" is not "120") — makes the whole parse fail rather than
    silently taking the part that did parse. A fader that lands on the wrong value because "one" was
    read off "one twenty" is worse than one that doesn't move at all.

    Returns `None` when `text` doesn't parse as a number at all.
    """
    if not text:
        return None
    t = text.strip().lower()
    if not t:
        return None

    # The literal fast path: "-3", "3.5", "41" typed or transcribed as digits.
    try:
        return float(t)
    except ValueError:
        pass

    # From here on it's word form. A hyphen only ever joins a compound ("twenty-three"); the literal
    # try above already handled a leading minus sign, so it's safe to flatten hyphens to spaces now.
    tokens = t.replace("-", " ").split()
    if not tokens:
        return None

    sign = 1.0
    i = 0
    if tokens[i] in ("minus", "negative"):
        sign = -1.0
        i += 1
    if i >= len(tokens):
        return None

    def literal(tok: str) -> float | None:
        try:
            return float(tok)
        except ValueError:
            return None

    if tokens[i] == "point":
        # A bare "point five" with no integer part — the integer part is implicitly zero, and the
        # "point" token itself is consumed below by the decimal-parsing block.
        int_part = 0.0
    else:
        v = literal(tokens[i])
        if v is not None:
            int_part = v
            i += 1
        elif tokens[i] in _TENS:
            int_part = float(_TENS[tokens[i]])
            i += 1
            if i < len(tokens) and tokens[i] in _ONES and _ONES[tokens[i]] < 10:
                int_part += _ONES[tokens[i]]
                i += 1
        elif tokens[i] in _ONES:
            int_part = float(_ONES[tokens[i]])
            i += 1
        else:
            return None

    if i < len(tokens) and tokens[i] == "point":
        i += 1
        digits = ""
        while i < len(tokens):
            tok = tokens[i]
            if tok in _ONES and _ONES[tok] < 10:
                digits += str(_ONES[tok])
                i += 1
            elif tok.isdigit():
                digits += tok
                i += 1
            else:
                break
        if not digits:
            return None
        int_part = float(f"{int(int_part)}.{digits}")

    if i != len(tokens):
        return None   # leftover words we can't account for — refuse rather than guess
    return sign * int_part


# --------------------------------------------------------------------------- ASR-noise normalisation

# Filler a push-to-talk mic reliably picks up: a breath ("uh"/"um"), a wake word said out of habit, or
# the app name spoken at the start because the user is talking *to* Logic ("logic, mute strings").
_LEADING_FILLERS = {"uh", "um", "hey", "logic"}

# Small, explicit, empirically observed mishearings from this project's own ASR testing. Kept short on
# purpose (see the module docstring in voiced.py and GESTURE_VOICE_PLAN.md) — a long table of guesses
# would start eating real track names.
_MISHEARINGS = [
    (re.compile(r"\bsolar\b"), "solo"),     # "solo" comes back as "solar" often enough to matter
    (re.compile(r"\bare am\b"), "arm"),     # "arm" gets split into two words
    (re.compile(r"\bfar\b"), "bar"),        # "go to bar" mis-heard as "go to far"
]


# Articles that show up in front of a track name in natural speech ("solo the strings") and would
# otherwise be passed through as part of the name, so that resolving it against Logic's real track list
# looks for a track literally called "the strings" and finds nothing.
_TARGET_ARTICLES = ("the ", "a ", "an ", "my ")


def clean_target(name: str | None) -> str | None:
    """Trim the words that are grammar rather than name. The rest is passed through verbatim — this
    module never guesses WHICH track a word means, it only stops handing on words that cannot be part
    of one."""
    if not name:
        return None
    t = name.strip().lower()
    changed = True
    while changed:
        changed = False
        for art in _TARGET_ARTICLES:
            if t.startswith(art) and len(t) > len(art):
                t, changed = t[len(art):].strip(), True
    t = re.sub(r"\s+(track|channel)$", "", t).strip()      # "solo the bass track"
    # "select track 4" must resolve as strip 4, not as a track literally named "track 4". Only strip the
    # leading word when a bare number follows it — "track vocals" is someone naming a track.
    m = re.match(r"^(?:track|channel|strip)\s+(\d+)$", t)
    if m:
        t = m.group(1)
    return t or None


def _normalize(text: str) -> str:
    t = (text or "").strip().lower()
    t = re.sub(r"[?.!,;:]+$", "", t).strip()          # trailing punctuation ASR likes to append
    t = re.sub(r"[^a-z0-9\s.\-]", " ", t)              # drop stray punctuation elsewhere
    t = re.sub(r"\s+", " ", t).strip()

    tokens = t.split()
    while tokens and tokens[0] in _LEADING_FILLERS:
        tokens.pop(0)
    t = " ".join(tokens)

    for pattern, replacement in _MISHEARINGS:
        t = pattern.sub(replacement, t)
    return re.sub(r"\s+", " ", t).strip()


# --------------------------------------------------------------------------- command grammar

# Verbs that must never be reachable from a gesture, ever. The caller (gestured) checks this before
# executing anything that came from `parse()` — a pinch or a fist must not be able to reach these, and
# in practice they never will, because nothing in this grammar binds them to a pose in the first place.
# This set is the belt to that suspenders.
DESTRUCTIVE = frozenset({"delete_track", "bounce", "save"})

# Whole-utterance commands: no target, no value, exactly one meaning.
_EXACT = {
    "play": "play",
    "stop": "stop",
    "record": "record",
    "go to start": "goto_start",
    "cycle on": "cycle_on",
    "cycle off": "cycle_off",
    "metronome on": "metronome_on",
    "metronome off": "metronome_off",
    "save": "save",
    "undo": "undo",
    "redo": "redo",
    "bounce": "bounce",
    "new audio track": "new_audio_track",
    "new instrument track": "new_instrument_track",
}

_DB_UNITS = {"db": "dB", "decibels": "dB", "decibel": "dB"}

_TRACK_VERB_RE = re.compile(r"^(select|mute|unmute|solo|unsolo|arm|disarm|rename)\s+(.+)$")
_AXIS_ON_RE = re.compile(r"^(volume|pan|param)\s+on\s+(.+)$")
_RIDE_RE = re.compile(r"^ride\s+(.+)$")
_VOLUME_TO_RE = re.compile(r"^volume\s+(.+?)\s+to\s+(.+)$")
_SET_TO_RE = re.compile(r"^set\s+(.+?)\s+to\s+(.+)$")
_PAN_DIR_RE = re.compile(r"^pan\s+(.+?)\s+(hard left|hard right|left|right)(?:\s+(.+))?$")
_UP_DOWN_RE = re.compile(r"^(.+?)\s+(down|up)\s+(.+)$")
_GO_TO_BAR_RE = re.compile(r"^go to bar\s+(.+)$")
_TEMPO_RE = re.compile(r"^tempo\s+(.+)$")
_DELETE_TRACK_RE = re.compile(r"^delete track\s+(.+)$")
_BYPASS_RE = re.compile(r"^bypass\s+(.+?)\s+on\s+(.+)$")
_PLUGIN_ON_RE = re.compile(r"^(.+?)\s+on\s+(.+)$")


def _strip_unit(s: str, units: dict[str, str]) -> tuple[str, str | None]:
    """Peel a trailing unit word off `s`, longest keyword first so "decibels" doesn't get shadowed by
    a coincidental shorter match. Returns the remainder (for `spoken_number`) and the canonical unit,
    or `(s, None)` when no known unit is present."""
    s = s.strip()
    for kw in sorted(units, key=len, reverse=True):
        if s == kw or s.endswith(" " + kw):
            return s[: -len(kw)].strip(), units[kw]
    return s, None


def _parse_inner(text: str) -> P.Intent | None:
    """Text to `P.Intent`, or `None` when nothing here matches (the caller's cue to fall through to the
    model). Must stay fast — see the module docstring — so this is regex and dict lookups only, no I/O
    and nothing that could block.
    """
    raw = text
    t = _normalize(text)
    if not t:
        return None

    if t in _EXACT:
        return P.Intent(verb=_EXACT[t], raw=raw)

    m = _GO_TO_BAR_RE.match(t)
    if m:
        n = spoken_number(m.group(1))
        return P.Intent(verb="goto_bar", value=n, raw=raw) if n is not None else None

    m = _TEMPO_RE.match(t)
    if m:
        n = spoken_number(m.group(1))
        return P.Intent(verb="tempo", value=n, raw=raw) if n is not None else None

    m = _DELETE_TRACK_RE.match(t)
    if m:
        track = m.group(1).strip()
        return P.Intent(verb="delete_track", target=track, raw=raw) if track else None

    m = _BYPASS_RE.match(t)
    if m:
        plugin, track = m.group(1).strip(), m.group(2).strip()
        if not plugin or not track:
            return None
        # `axis` doubles as "which sub-thing on the target" here — Intent has no dedicated plugin
        # field, and this is exactly the role `axis` already plays for volume/pan/param bindings.
        return P.Intent(verb="plugin_bypass", target=track, axis=plugin, raw=raw)

    m = _AXIS_ON_RE.match(t)
    if m:
        verb, track = m.group(1), m.group(2).strip()
        return P.Intent(verb=verb, target=track, raw=raw) if track else None

    m = _RIDE_RE.match(t)
    if m:
        # "Ride the fader" has always meant volume; there's no separate axis for it.
        track = m.group(1).strip()
        return P.Intent(verb="volume", target=track, raw=raw) if track else None

    m = _VOLUME_TO_RE.match(t)
    if m:
        track, rest = m.group(1).strip(), m.group(2).strip()
        if not track:
            return None
        num_phrase, _unit = _strip_unit(rest, _DB_UNITS)   # "dB" is optional here — context is enough
        value = spoken_number(num_phrase)
        return P.Intent(verb="volume", target=track, value=value, unit="dB", raw=raw) \
            if value is not None else None

    m = _SET_TO_RE.match(t)
    if m:
        track, rest = m.group(1).strip(), m.group(2).strip()
        if not track:
            return None
        num_phrase, unit = _strip_unit(rest, _DB_UNITS)
        if unit is None:
            # "set strings to 3" — dB? pan? Without "volume" or a unit word this is a real ambiguity,
            # not a non-match, so it refuses exactly like an unknown command would.
            return None
        value = spoken_number(num_phrase)
        return P.Intent(verb="volume", target=track, value=value, unit=unit, raw=raw) \
            if value is not None else None

    m = _PAN_DIR_RE.match(t)
    if m:
        track, direction, num_phrase = m.group(1).strip(), m.group(2), m.group(3)
        if not track:
            return None
        if direction.startswith("hard"):
            magnitude = 100.0
        else:
            if not num_phrase:
                return None
            magnitude = spoken_number(num_phrase)
            if magnitude is None:
                return None
        sign = -1.0 if direction.endswith("left") else 1.0
        return P.Intent(verb="pan", target=track, value=sign * magnitude, unit="pan", raw=raw)

    m = _UP_DOWN_RE.match(t)
    if m:
        track, direction, rest = m.group(1).strip(), m.group(2), m.group(3).strip()
        if not track:
            return None
        num_phrase, unit = _strip_unit(rest, _DB_UNITS)
        if unit is None:
            return None   # "strings down two" with no unit — ambiguous, let the model ask what "two" means
        magnitude = spoken_number(num_phrase)
        if magnitude is None:
            return None
        verb = "volume_down" if direction == "down" else "volume_up"
        return P.Intent(verb=verb, target=track, value=magnitude, unit=unit, raw=raw)

    m = _TRACK_VERB_RE.match(t)
    if m:
        verb, track = m.group(1), m.group(2).strip()
        return P.Intent(verb=verb, target=track, raw=raw) if track else None

    m = _PLUGIN_ON_RE.match(t)
    if m:
        plugin, track = m.group(1).strip(), m.group(2).strip()
        if not plugin or not track:
            return None
        return P.Intent(verb="plugin_add", target=track, axis=plugin, raw=raw)

    return None


def parse(text: str) -> P.Intent | None:
    """Public entry point: parse, then tidy the track name.

    Kept as a wrapper rather than threading the cleanup through every branch, because there are a dozen
    places a target is captured and only one place it leaves.
    """
    intent = _parse_inner(text)
    if intent is not None:
        intent.target = clean_target(intent.target)
    return intent
