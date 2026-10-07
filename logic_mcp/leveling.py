"""Turn stem measurements into concrete gain and pan moves.

The reference stem (usually the lead vocal) defines the balance: every other stem is placed at a fixed
offset from it by role, so the mix keeps its character instead of being flattened to a number. Every
suggestion carries the measurement it came from, so a human can disagree with the reasoning rather than
just the result.

**The reference's own fader is not guaranteed to stay at 0.** It is fixed *relative* to the others, which
is what preserves the balance — but nothing is ever boosted into the ceiling, so when any stem wants a
boost the whole set is shifted down by that amount instead, the reference included. A reference at -14 LUFS
next to a guitar wanting +4 dB comes back as reference -4 dB / guitar 0 dB: the same 4 dB relationship, with
no stem pushed up. Each move reports `shifted_by_db` when this happened, and `gain_uncapped_db` is the move
before the shift, so "this stem wanted to be louder" is still visible in the output.

A consequence worth knowing: after a downward shift no `gain_db` is ever positive. That is deliberate, not
a clamp bug — read `gain_uncapped_db` when you want the un-shifted intent.
"""
from __future__ import annotations

import re

from . import audio

# Offsets in LU relative to the reference stem. Negative sits behind it.
ROLE_TARGETS = {
    "lead_vocal": 0.0, "vocal": -1.0, "backing_vocal": -6.0,
    "kick": -2.0, "snare": -3.0, "drums": -2.0, "percussion": -7.0,
    "bass": -2.5,
    "guitar": -6.0, "keys": -7.0, "piano": -6.0, "strings": -7.0, "pad": -9.0, "synth": -7.0,
    "fx": -10.0, "other": -6.0,
}
ROLE_TARGETS.setdefault("percussion", -7.0)

# Where each role usually sits, and how far it may be moved. Low content stays centred or it loses power.
ROLE_PAN = {
    "lead_vocal": (0, 0), "vocal": (0, 0), "backing_vocal": (0, 45),
    "kick": (0, 0), "snare": (0, 8), "drums": (0, 0), "percussion": (0, 40),
    "bass": (0, 0),
    "guitar": (0, 40), "keys": (0, 30), "piano": (0, 25), "strings": (0, 35), "pad": (0, 40), "synth": (0, 35),
    "fx": (0, 50), "other": (0, 25),
}

# Order matters: the first match wins, so the specific roles come before the general ones.
ROLE_PATTERNS = [
    (r"\b(lead\s*vox|lead\s*vocals?|lv)\b", "lead_vocal"),
    (r"\b(bgvs?|backing\s*(vox|vocals?)?|harmonies|harmony|chorus\s*vox|dbl|double)\b", "backing_vocal"),
    (r"\b(vox|vocals?|voc|sing\w*)\b", "vocal"),
    (r"\b(kicks?|bd)\b", "kick"),
    (r"\b(snares?|sd)\b", "snare"),
    (r"\b(drums?|dr|beats?|kit|overheads?|oh|rooms?|toms?|floor\s*tom|rack\s*tom|ride|crash)\b", "drums"),
    (r"\b(perc\w*|shaker|tabla|conga|cymbal\w*|hats?|hi\s*hats?)\b", "percussion"),
    (r"\b(bass|bs|808|sub)\b", "bass"),
    (r"\b(gtrs?|guitars?|gits?|acoustic|elec)\b", "guitar"),
    (r"\b(pianos?|pno)\b", "piano"),
    (r"\b(keys?|rhodes|organ|harmonium|accordion|wurli|clav)\b", "keys"),
    (r"\b(synths?|lead\s*synth)\b", "synth"),
    (r"\b(strings?|violin|cello|viola)\b", "strings"),
    (r"\b(pads?|atmos\w*|ambien\w*)\b", "pad"),
    (r"\b(fx|risers?|impacts?|sfx|whoosh)\b", "fx"),
]


def guess_role(name: str) -> str:
    n = re.sub(r"[_\-().]", " ", name.lower())
    for pat, role in ROLE_PATTERNS:
        if re.search(pat, n):
            return role
    return "other"


def _clean_name(name: str) -> str:
    """'My Song ep 03 (VOX)Stems.wav' -> 'VOX'"""
    stem = re.sub(r"\.(wav|aif|aiff|caf|mp3)$", "", name, flags=re.I)
    m = re.search(r"\(([^)]+)\)", stem)
    return (m.group(1) if m else stem).strip()


def analyse(paths: list[str], max_seconds: float = 600.0) -> list[dict]:
    out = []
    for p in paths:
        m = audio.measure(p, max_seconds)
        d = m.to_dict()
        d["short"] = _clean_name(m.name)
        d["role"] = guess_role(d["short"]) if guess_role(d["short"]) != "other" else guess_role(m.name)
        out.append(d)
    return out


def plan(stems: list[dict], reference: str | None = None, max_move_db: float = 9.0,
         headroom_db: float = -1.0) -> dict:
    """Compute a gain (and pan) move per stem. Nothing is applied here — this is the proposal."""
    usable = [s for s in stems if not s["silent"] and s["lufs"] is not None]
    if not usable:
        return {"error": "no measurable stems", "stems": stems}

    if reference:
        hits = [s for s in usable if reference.lower() == s["short"].lower()] or \
               [s for s in usable if reference.lower() in s["short"].lower() or reference.lower() in s["name"].lower()]
        if len(hits) != 1:
            return {"error": f"reference {reference!r} matched {len(hits)} stems — name it exactly",
                    "candidates": [s["short"] for s in (hits or usable)]}
        ref = hits[0]
    else:
        ref = next((s for s in usable if s["role"] == "lead_vocal"), None) \
              or next((s for s in usable if s["role"] == "vocal"), None) \
              or max(usable, key=lambda s: s["lufs"])
    ref_lufs = ref["lufs"]

    moves = []
    for s in usable:
        role = s["role"]
        # the reference defines the balance, so by construction it does not move
        target_off = 0.0 if s is ref else ROLE_TARGETS.get(role, ROLE_TARGETS["other"])
        current_off = s["lufs"] - ref_lufs
        gain = target_off - current_off
        clamped = max(-max_move_db, min(max_move_db, gain))

        # keep the loudest peak under the headroom line, then re-clamp so the guard can only ever
        # reduce the move — never hand back budget the clamp had already taken away
        tp = s["true_peak_db"]
        clipped_by = None
        if tp is not None and tp + clamped > headroom_db:
            reduce = (tp + clamped) - headroom_db
            clamped = max(-max_move_db, clamped - reduce)
            clipped_by = round(reduce, 2)

        home, spread = ROLE_PAN.get(role, ROLE_PAN["other"])
        corr = s["correlation"]
        pan_note = None
        pan_action = "set"
        if spread == 0:
            pan = 0
            pan_note = "centred — low or lead content"
        elif corr is not None and corr < 0.6:
            pan = None
            pan_action = "leave"            # already a wide source; centring it would collapse the image
            pan_note = f"already wide (correlation {corr:.2f}) — left untouched"
        else:
            pan = None                      # decided in the spreading pass below
        moves.append({
            "name": s["short"], "file": s["name"], "role": role,
            "lufs": round(s["lufs"], 1), "true_peak_db": round(s["true_peak_db"], 1) if s["true_peak_db"] is not None else None,
            "crest_db": round(s["crest_db"], 1) if s["crest_db"] is not None else None,
            "correlation": round(corr, 2) if corr is not None else None,
            "relative_now": round(current_off, 1), "relative_target": target_off,
            "gain_db": round(clamped, 1), "gain_uncapped_db": round(gain, 1),
            "reduced_for_headroom_db": clipped_by,
            "pan": pan, "pan_note": pan_note, "pan_spread": spread, "pan_action": pan_action,
        })

    # Shift the whole set down so nothing is pushed up into the ceiling. Bounded by max_move_db so the
    # shift cannot smuggle in a bigger move than the caller allowed, including on the reference stem.
    biggest = max((m["gain_db"] for m in moves), default=0.0)
    shift = 0.0
    if biggest > 0:
        shift = min(biggest, max_move_db)
        for m in moves:
            # the reference is shifted too — that is what keeps the balance intact while nothing is
            # pushed up into the ceiling; see the module docstring
            m["gain_db"] = round(max(-max_move_db, m["gain_db"] - shift), 1)
            m["shifted_by_db"] = round(-shift, 1)

    # spread the pannable stems alternately left and right, widest allowance first
    pannable = [m for m in moves if m["pan"] is None and m["pan_action"] == "set"]
    pannable.sort(key=lambda m: -m["pan_spread"])
    n = len(pannable)
    if n == 1:
        pannable[0]["pan"] = 0
        pannable[0]["pan_note"] = "only pannable source — left centred"
    else:
        pairs = n // 2
        for i, m in enumerate(pannable):
            if n % 2 == 1 and i == n - 1:          # odd one out sits in the middle, not off to one side
                m["pan"] = 0
                m["pan_note"] = "centred to keep the image balanced"
                continue
            side = -1 if i % 2 == 0 else 1
            depth = 0.6 + 0.4 * ((i // 2) / max(1, pairs - 1)) if pairs > 1 else 1.0
            m["pan"] = int(round(side * m["pan_spread"] * depth))
            m["pan_note"] = "spread for separation"

    return {
        "reference": {"name": ref["short"], "role": ref["role"], "lufs": round(ref_lufs, 1),
                      "moved_by_db": round(-shift, 1) if shift else 0.0,
                      "note": ("the whole set was shifted down so nothing is boosted into the ceiling; "
                               "the reference moved with it and the balance is unchanged") if shift else
                              "reference fader unchanged"},
        "headroom_db": headroom_db,
        "global_shift_db": round(-shift, 1) if shift else 0.0,
        "moves": sorted(moves, key=lambda m: m["gain_db"]),
        "skipped": [{"name": s["short"], "reason": "silent" if s["silent"] else "unmeasurable"}
                    for s in stems if s not in usable],
    }


def summarise(p: dict) -> str:
    if "error" in p:
        return p["error"]
    lines = [f"reference: {p['reference']['name']} ({p['reference']['role']}) at {p['reference']['lufs']} LUFS"]
    for m in p["moves"]:
        bits = [f"{m['name']:<10s} {m['role']:<12s} {m['lufs']:>6.1f} LUFS", f"gain {m['gain_db']:+5.1f} dB"]
        if m["pan"]:
            bits.append(f"pan {m['pan']:+d}")
        if m["reduced_for_headroom_db"]:
            bits.append(f"(held back {m['reduced_for_headroom_db']} dB for headroom)")
        lines.append("  " + "   ".join(bits))
    return "\n".join(lines)
