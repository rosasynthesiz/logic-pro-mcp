"""Classify tracks by instrument from their names, for colouring, grouping and stacking.

Sample-library names carry the instrument in them ("AU_VBP_115_drum_full_triplet_1x120bpm"), so most of a
loop-built session can be sorted without listening to it. Order matters: the first pattern that matches wins,
so specific instruments come before the families that would otherwise swallow them.
"""
from __future__ import annotations

import re

# family -> (palette colour name, ordered regex patterns)
FAMILIES: list[tuple[str, str, list[str]]] = [
    # vocal percussion is percussion, so it must be caught before the vocal family
    ("percussion", "orange", [r"vocal percussion", r"vocal perc\b"]),
    ("vocals", "blue", [r"\bvox\b", r"\bvocals?\b", r"\bsing", r"\bchoir\b", r"\bacapella\b"]),
    ("drums", "dark red", [r"\bdrum", r"\bkick\b", r"\bsnare\b", r"\bhat\b", r"\bhats\b", r"\bkit\b",
                            r"\btom\b", r"\btoms\b", r"\bbreakbeat\b", r"\bdrumset\b", r"\bwar_drums\b"]),
    ("percussion", "orange", [r"\bperc\b", r"percussion", r"\bconga\b", r"\bbongo\b", r"\bshaker\b", r"\btabla\b",
                               r"\bdarbuka\b", r"\bthavil\b", r"\budukai\b", r"\burumi\b", r"\bmridang", r"\bduff\b",
                               r"\btama\b", r"\bdjembe\b", r"\btimbale", r"\btalking_drum\b", r"\btympani\b", r"\btribal\b"]),
    ("bass", "indigo", [r"\bbass\b", r"\b808\b", r"\bsub\b", r"bass_synth", r"synth_bass", r"\bbassline\b"]),
    ("guitar", "green", [r"\bguitar", r"\bgtr\b", r"\bbaglama\b", r"\bstrum", r"\bacoustic\b", r"\belectric_guitar\b"]),
    ("strings", "violet", [r"\bstrings?\b", r"\bviolin\b", r"\bcello\b", r"\bviola\b", r"\borchestra", r"\bensemble\b"]),
    ("woodwind", "teal", [r"\bwoodwind", r"\bflute\b", r"\bclarinet\b", r"\boboe\b", r"\bsax\b", r"\bbassoon\b"]),
    ("brass", "amber", [r"\btrumpet\b", r"\bbrass\b", r"\btrombone\b", r"\bhorn\b", r"\btuba\b"]),
    ("keys", "azure", [r"\bpiano\b", r"\bkeys?\b", r"\brhodes\b", r"\borgan\b", r"\bharmonium\b", r"\baccord",
                        r"\bvibraphone\b", r"\bmarimba\b", r"\bglocken", r"\btwinkle\b", r"\bmusicalpiano\b"]),
    ("synth", "magenta", [r"\bsynth\b", r"\barp\b", r"\bpad\b", r"\blead\b", r"\bpsy\b", r"\bsaw\b", r"\bpluck\b"]),
    ("fx", "grape", [r"\bfx\b", r"\bsfx\b", r"\bimpact", r"\briser\b", r"\bwhoosh\b", r"\bsweep\b", r"\bdrone\b",
                      r"\bambien", r"\bambius\b", r"\bhorror\b", r"\bscare\b", r"\bsiren\b", r"\bstab\b", r"\bcartoon\b",
                      r"\boneshot\b", r"\bone shot\b", r"\bghost", r"\bfill\b", r"\bnegative\b", r"\bbounce out\b"]),
]

# names that are clearly a full mix / bounce rather than an instrument
MIX_PATTERNS = [r"\bfirst half\b", r"\bmid\b$", r"\bdemo\b", r"\bep[- ]?\d", r"\brr\b", r"manorama mansion ep",
                r"\btitle\b", r"\bfull mix\b", r"\bmaster\b"]


# Sample libraries prefix their names with a vendor code and a tempo: "VOX_LCH_128_bass_synth_sub_big".
# That leading VOX is a publisher, not a vocal, so the prefix is removed before anything is matched.
VENDOR_PREFIX = re.compile(r"^(?:[A-Z0-9]{2,9}[_ ]){1,4}\d{2,3}[_ ]")


def _norm(name: str) -> str:
    """Library names use underscores, camel case and pitch/bpm suffixes; flatten them into words."""
    s = VENDOR_PREFIX.sub("", name)
    s = re.sub(r"\.[a-z0-9]{2,4}$", "", s, flags=re.I)
    s = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", s)                 # MusicalPiano -> Musical Piano
    s = re.sub(r"[_\-.()\[\]]+", " ", s)
    s = re.sub(r"\b\d+x\d+bpm\b|\b\d+bpm\b|\b[+-]?\d+pitchadjusted\b", " ", s, flags=re.I)
    return re.sub(r"\s+", " ", s).strip().lower()


def classify(name: str) -> tuple[str, str | None]:
    """(family, colour). family 'unknown' when nothing matches — those are left alone."""
    n = _norm(name)
    if re.fullmatch(r"[0-9a-f]{20,}", n.replace(" ", "")):
        return "unknown", None                                  # hash-named import
    for pat in MIX_PATTERNS:
        if re.search(pat, n):
            return "mix", "darkest blue"
    for family, colour, pats in FAMILIES:
        for pat in pats:
            if re.search(pat, n):
                return family, colour
    if re.fullmatch(r"(audio|inst|track)\s*\d*", n):
        return "unknown", None
    return "unknown", None


def group_tracks(tracks: list[dict]) -> dict:
    """{family: [track, ...]} in the order the families are declared."""
    order = ["vocals", "drums", "percussion", "bass", "guitar", "strings", "woodwind", "brass",
             "keys", "synth", "fx", "mix", "unknown"]
    out: dict[str, list] = {k: [] for k in order}
    for t in tracks:
        fam, colour = classify(t["name"])
        out.setdefault(fam, []).append(t | {"family": fam, "colour": colour})
    return {k: v for k, v in out.items() if v}


# short prefix used in the renamed track, per family
SHORT = {"vocals": "Vox", "drums": "Drums", "percussion": "Perc", "bass": "Bass", "guitar": "Gtr",
         "strings": "Strings", "woodwind": "Wind", "brass": "Brass", "keys": "Keys", "synth": "Synth",
         "fx": "FX", "mix": "Mix", "unknown": "Others"}

# the words worth keeping as a hint, longest/most specific first
HINTS = [
    "darbuka", "thavil", "udukai", "urumi", "mridangam", "tabla", "conga", "bongo", "djembe", "shaker",
    "timbale", "talking drum", "tympani", "tribal", "duff", "tama", "cajon", "clap", "snap", "whisper",
    "kick", "snare", "hat", "hats", "tom", "toms", "ride", "crash", "overhead", "drumset", "kit", "breakbeat",
    "sub", "808", "upright", "electric", "acoustic", "spanish", "nylon", "baglama", "sitar", "oud",
    "violin", "cello", "viola", "orchestra", "ensemble", "pizzicato",
    "flute", "clarinet", "oboe", "sax", "bassoon", "recorder",
    "trumpet", "trombone", "horn", "tuba",
    "piano", "rhodes", "organ", "harmonium", "accordion", "vibraphone", "marimba", "glockenspiel", "twinkle",
    "arp", "pad", "pluck", "saw", "psy", "lead",
    "whoosh", "riser", "sweep", "impact", "drone", "ambience", "horror", "siren", "stab", "scare", "cartoon",
    "hook", "chant", "choir", "harmony",
    "swing", "shuffle", "triplet", "latin", "mambo", "samba", "brazil", "goa", "gospel", "jazzy", "funk",
]


def hint(name: str) -> str:
    """A short, human-meaningful descriptor pulled out of a sample-library name.

    Takes no family: HINTS is scanned in full regardless, and carrying a `family` argument that nothing
    read implied a family-awareness this has never had.
    """
    n = _norm(name)
    found = [h for h in HINTS if re.search(r"\b" + re.escape(h) + r"\b", n)]
    if not found:
        return ""
    # prefer the most specific (longest) and cap at two words so names stay short
    found.sort(key=len, reverse=True)
    picked = found[:2] if len(found[0]) <= 6 else found[:1]
    return " ".join(w.title() for w in picked)


def rename_plan(tracks: list[dict]) -> list[dict]:
    """One new name per track: '<Family> NN <Hint>', numbered per family in track order."""
    counts: dict[str, int] = {}
    plan = []
    for t in tracks:
        fam, colour = classify(t["name"])
        counts[fam] = counts.get(fam, 0) + 1
        short = SHORT.get(fam, "Others")
        h = hint(t["name"]) if fam != "unknown" else ""
        new = f"{short} {counts[fam]:02d}" + (f" {h}" if h else "")
        plan.append({"index": t["index"], "old": t["name"], "new": new[:40], "family": fam, "colour": colour})
    return plan
