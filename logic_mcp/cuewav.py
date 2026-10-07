"""Read and write RIFF/WAVE cue points with labels ("cue "/LIST-adtl chunks).

Why: Logic Pro has Navigate > Other > Import Marker from Audio File. If a WAV file carries cue points
and their labels, that one menu command turns them into real Logic markers -- hundreds at once instead
of hundreds of manual marker-drop-and-rename steps. This module only produces/reads the WAV side of that;
nothing here launches or touches Logic.

No AI, no language dependency: cue positions are plain sample offsets and labels are opaque strings. The
same code writes a Tamil label, an English one, or a mix, with the same bytes-in bytes-out logic. The one
real question is *encoding* -- the RIFF spec predates UTF-8, cue labels ("labl" chunks) are just
length-prefixed byte strings with no encoding tag, and it is genuinely unknown up front what Logic's
importer assumes. So encoding is a parameter, not a fixed choice, and this module can emit the same cue
set in several encodings side by side so that question can be settled by testing in Logic directly.

Chunk layout produced by write_cues, in order: RIFF/WAVE, "fmt " (copied verbatim from the source file),
"fact" (copied verbatim if the source had one -- relevant for non-PCM formats), "data" (copied verbatim),
"cue " (point table), "LIST"/"adtl" (containing one "labl" per cue, and one "ltxt" per cue that was given
a length, for region-style markers). Any other chunks in the source (existing cue/LIST metadata, "bext",
etc.) are dropped -- this module writes markers fresh rather than merging with what's there.

Every chunk is written id+size+body(+ pad byte if size is odd), which is the one thing that reliably
makes hand-built RIFF files this shows a corrupt reader instead of a marker list: get a size field or a
padding byte wrong and everything after it in the file misparses. write_cues re-parses what it just wrote
before returning, so a broken file is caught here rather than in Logic.
"""
from __future__ import annotations

import os
import struct
from typing import Any

# --- low-level RIFF ---------------------------------------------------------------------------------

def _pack_chunk(chunk_id: bytes, body: bytes) -> bytes:
    assert len(chunk_id) == 4, chunk_id
    out = chunk_id + struct.pack("<I", len(body)) + body
    if len(body) % 2:                                  # RIFF chunks always sit on even byte boundaries
        out += b"\x00"
    return out


def _parse_riff(buf: bytes) -> list[tuple[bytes, bytes]]:
    """Top-level chunks as [(id, body), ...], in file order. Stops cleanly on a truncated tail chunk."""
    if len(buf) < 12 or buf[0:4] != b"RIFF" or buf[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    chunks = []
    pos = 12
    n = len(buf)
    while pos + 8 <= n:
        cid = buf[pos:pos + 4]
        size = struct.unpack_from("<I", buf, pos + 4)[0]
        body_start = pos + 8
        body_end = body_start + size
        if body_end > n:
            break                                       # corrupt/truncated trailing chunk -- ignore it
        chunks.append((cid, buf[body_start:body_end]))
        pos = body_end + (size & 1)
    return chunks


def _sub_chunks(buf: bytes) -> list[tuple[bytes, bytes]]:
    """Same idea as _parse_riff but for a chunk body that is itself a run of sub-chunks (e.g. LIST/adtl
    content after the 4-byte list-type tag)."""
    chunks = []
    pos = 0
    n = len(buf)
    while pos + 8 <= n:
        cid = buf[pos:pos + 4]
        size = struct.unpack_from("<I", buf, pos + 4)[0]
        body_start = pos + 8
        body_end = body_start + size
        if body_end > n:
            break
        chunks.append((cid, buf[body_start:body_end]))
        pos = body_end + (size & 1)
    return chunks


# --- encodings ---------------------------------------------------------------------------------------
# RIFF "labl" text has no encoding tag. These are the three strategies worth testing against Logic.

_CODEPAGE = {"utf-8": 65001, "utf-8-fallback": 65001, "latin-1": 28591, "cp1252": 1252}


def _encoder(encoding: str):
    """Returns fn(str) -> (bytes, used_encoding, fallback_or_lossy: bool)."""
    if encoding == "utf-8":
        return lambda s: (s.encode("utf-8"), "utf-8", False)
    if encoding == "latin-1":
        def enc(s: str):
            lossy = any(ord(ch) > 255 for ch in s)      # anything outside Latin-1 becomes '?' (0x3F)
            return s.encode("latin-1", errors="replace"), "latin-1", lossy
        return enc
    if encoding == "cp1252-with-utf8-fallback":
        def enc(s: str):
            try:
                return s.encode("cp1252"), "cp1252", False
            except UnicodeEncodeError:
                return s.encode("utf-8"), "utf-8-fallback", True
        return enc
    raise ValueError(f"unknown encoding {encoding!r} (use 'utf-8', 'latin-1', or 'cp1252-with-utf8-fallback')")


def _decode_label(raw: bytes) -> tuple[str, str]:
    """Best-effort decode for a label we did not write ourselves: try strict UTF-8, then strict cp1252,
    then Latin-1 (which never fails -- it maps every byte to a codepoint). Reports which one worked so
    callers can tell a confident decode from a guess."""
    if not raw:
        return "", "empty"
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    try:
        return raw.decode("cp1252"), "cp1252"
    except UnicodeDecodeError:
        pass
    return raw.decode("latin-1"), "latin-1-fallback"


# --- public API ----------------------------------------------------------------------------------------

def write_cues(src_wav: str, dst_wav: str, cues: list[dict[str, Any]], *, encoding: str = "utf-8") -> dict:
    """Copy src_wav's audio verbatim into dst_wav and add a cue point + label per entry in `cues`.

    cues: [{"position": <seconds (float) or samples (int)>, "label": str, "length": optional, same units
           as position -- when given, a region-style "ltxt" chunk is written alongside the label}, ...]

    encoding: "utf-8" | "latin-1" | "cp1252-with-utf8-fallback" -- see module docstring. Every label is
    encoded independently, so "cp1252-with-utf8-fallback" can genuinely mix cp1252 and utf-8 labels in one
    file if some labels fit cp1252 and others don't.

    Cues are written in ascending position order with sequential ids starting at 1, regardless of the
    order given -- most importers (Logic included, going by its own marker list) expect a time-ordered
    cue table. Positions past the end of the audio are clamped into range rather than rejected.

    Returns a report dict; raises ValueError on a malformed source file or unknown encoding.
    """
    with open(src_wav, "rb") as f:
        buf = f.read()
    chunks = _parse_riff(buf)

    fmt_body = next((b for cid, b in chunks if cid == b"fmt "), None)
    data_body = next((b for cid, b in chunks if cid == b"data"), None)
    fact_body = next((b for cid, b in chunks if cid == b"fact"), None)
    if fmt_body is None or data_body is None:
        raise ValueError(f"{src_wav}: missing fmt or data chunk -- not a readable WAV file")

    _, num_channels, sample_rate, _, block_align, _ = struct.unpack_from("<HHIIHH", fmt_body, 0)
    if block_align == 0:
        raise ValueError(f"{src_wav}: fmt chunk reports block_align=0")
    total_frames = len(data_body) // block_align

    def to_samples(v) -> int:
        return int(v) if isinstance(v, int) else int(round(float(v) * sample_rate))

    norm = []
    for c in cues:
        pos = max(0, min(to_samples(c["position"]), total_frames))
        length = None
        if c.get("length") is not None:
            length = to_samples(c["length"])
            if length < 0:
                raise ValueError(f"cue {c.get('label')!r}: negative length {c['length']!r}")
            # Clamp like position: an ltxt region running past the end of the data chunk makes the file
            # inconsistent, and readers disagree about what to do with it.
            length = min(length, total_frames - pos)
        norm.append({"position_samples": pos, "label": c["label"], "length_samples": length})
    norm.sort(key=lambda c: c["position_samples"])

    encode = _encoder(encoding)

    cue_entries = bytearray()
    labl_chunks = bytearray()
    ltxt_chunks = bytearray()
    report_cues = []
    fallback_count = 0
    for i, c in enumerate(norm, start=1):
        cue_entries += struct.pack("<II4sIII", i, c["position_samples"], b"data", 0, 0, c["position_samples"])

        label_bytes, used_encoding, lossy_or_fallback = encode(c["label"])
        if lossy_or_fallback:
            fallback_count += 1
        labl_chunks += _pack_chunk(b"labl", struct.pack("<I", i) + label_bytes + b"\x00")

        if c["length_samples"] is not None:
            ltxt_body = struct.pack("<II4sHHHH", i, c["length_samples"], b"rgn ", 0, 0, 0,
                                     _CODEPAGE.get(used_encoding, 0))
            ltxt_chunks += _pack_chunk(b"ltxt", ltxt_body)

        report_cues.append({
            "id": i, "position_samples": c["position_samples"],
            "position_seconds": round(c["position_samples"] / sample_rate, 6),
            "label": c["label"], "label_bytes_hex": label_bytes.hex(),
            "encoded_with": used_encoding, "lossy_or_fallback": lossy_or_fallback,
        })

    cue_chunk = _pack_chunk(b"cue ", struct.pack("<I", len(norm)) + bytes(cue_entries)) if norm else b""
    list_chunk = _pack_chunk(b"LIST", b"adtl" + bytes(labl_chunks) + bytes(ltxt_chunks)) if norm else b""

    body = b"WAVE" + _pack_chunk(b"fmt ", fmt_body)
    if fact_body is not None:
        body += _pack_chunk(b"fact", fact_body)
    body += _pack_chunk(b"data", data_body) + cue_chunk + list_chunk
    out_bytes = _pack_chunk(b"RIFF", body)

    os.makedirs(os.path.dirname(dst_wav) or ".", exist_ok=True)
    with open(dst_wav, "wb") as f:
        f.write(out_bytes)

    # verify by re-parsing what was actually written, not what we think we wrote
    verify_error = None
    roundtrip = []
    try:
        roundtrip = read_cues(dst_wav)
        if len(roundtrip) != len(norm):
            verify_error = f"wrote {len(norm)} cues, read back {len(roundtrip)}"
    except Exception as e:                              # noqa: BLE001 -- reported, not swallowed
        verify_error = str(e)

    return {
        "path": dst_wav, "encoding": encoding, "sample_rate": sample_rate, "channels": num_channels,
        "total_frames": total_frames, "num_cues": len(norm), "cues": report_cues,
        "fallback_or_lossy_count": fallback_count, "bytes_written": len(out_bytes),
        "verified": verify_error is None, "verify_error": verify_error,
        "roundtrip_labels": [r["label"] for r in roundtrip],
    }


def write_cues_multi(src_wav: str, dst_dir: str, cues: list[dict[str, Any]],
                      encodings: tuple[str, ...] = ("utf-8", "latin-1", "cp1252-with-utf8-fallback"),
                      basename: str = "cues") -> dict[str, dict]:
    """Write the SAME cue set to dst_dir once per encoding, e.g. cues_utf-8.wav / cues_latin-1.wav / ...
    so the orchestrator can import each into Logic and compare. Returns {encoding: write_cues() report}."""
    os.makedirs(dst_dir, exist_ok=True)
    out = {}
    for enc in encodings:
        dst = os.path.join(dst_dir, f"{basename}_{enc.split('-with-')[0]}.wav")
        out[enc] = write_cues(src_wav, dst, cues, encoding=enc)
    return out


def read_cues(path: str) -> list[dict]:
    """Read cue points + labels back out of a WAV file. Does not assume who wrote it or in what
    encoding -- see _decode_label. Returns [] if the file has no "cue " chunk."""
    with open(path, "rb") as f:
        buf = f.read()
    chunks = _parse_riff(buf)

    fmt_body = next((b for cid, b in chunks if cid == b"fmt "), None)
    if fmt_body is None:
        raise ValueError(f"{path}: missing fmt chunk")
    sample_rate = struct.unpack_from("<I", fmt_body, 4)[0]

    cue_body = next((b for cid, b in chunks if cid == b"cue "), None)
    if not cue_body:
        return []

    labels: dict[int, bytes] = {}
    lengths: dict[int, int] = {}
    for cid, body in chunks:
        if cid != b"LIST" or body[:4] != b"adtl":
            continue
        for scid, sbody in _sub_chunks(body[4:]):
            if scid == b"labl":
                lbl_id = struct.unpack_from("<I", sbody, 0)[0]
                text = sbody[4:]
                if text.endswith(b"\x00"):
                    text = text[:-1]                    # single null terminator, per spec
                labels[lbl_id] = text
            elif scid == b"ltxt":
                lid, slen = struct.unpack_from("<II", sbody, 0)
                lengths[lid] = slen

    out = []
    n = struct.unpack_from("<I", cue_body, 0)[0]
    off = 4
    for _ in range(n):
        cue_id, position, _fcc, _chunk_start, _block_start, sample_offset = \
            struct.unpack_from("<II4sIII", cue_body, off)
        off += 24
        raw = labels.get(cue_id, b"")
        text, decoded_as = _decode_label(raw)
        out.append({
            "id": cue_id, "position_samples": sample_offset,
            "position_seconds": round(sample_offset / sample_rate, 6) if sample_rate else None,
            "label": text, "label_bytes_hex": raw.hex(), "decoded_as": decoded_as,
            "length_samples": lengths.get(cue_id),
        })
    return out
