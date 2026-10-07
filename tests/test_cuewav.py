"""Pins down logic_mcp.cuewav: writing RIFF cue points + labels into a WAV and reading them back.

The whole point of this module is that Navigate > Other > Import Marker from Audio File in Logic can
turn a cue list into real markers. If write_cues silently corrupts the fmt/data chunks, or mangles a
label's bytes, Logic either refuses the file or shows garbage marker names -- and there is no
"half-broken" outcome that is safe to ship. These tests exercise the round trip byte-exactly, including
the hard-won property that non-Latin (Tamil) UTF-8 labels survive unchanged.
"""
from __future__ import annotations

import struct

import numpy as np
import pytest

from logic_mcp import cuewav


def _fmt_and_data(path: str) -> tuple[bytes, bytes]:
    """Pull the raw fmt/data chunk bodies out of a WAV file, for byte-exact comparison."""
    with open(path, "rb") as f:
        buf = f.read()
    chunks = cuewav._parse_riff(buf)
    fmt_body = next(b for cid, b in chunks if cid == b"fmt ")
    data_body = next(b for cid, b in chunks if cid == b"data")
    return fmt_body, data_body


def test_round_trip_positions_and_labels(write_wav):
    src = write_wav("src.wav", np.linspace(-0.5, 0.5, 16000, dtype=np.float32))
    dst = src.replace("src.wav", "out.wav")

    cues = [
        {"position": 0.5, "label": "verse"},
        {"position": 0.75, "label": "chorus"},
    ]
    report = cuewav.write_cues(src, dst, cues)

    assert report["verified"] is True
    assert report["verify_error"] is None
    assert report["num_cues"] == 2

    read_back = cuewav.read_cues(dst)
    assert [c["label"] for c in read_back] == ["verse", "chorus"]
    # positions are stored as sample offsets; 0.5s / 0.75s at SR=16000
    assert [c["position_samples"] for c in read_back] == [8000, 12000]


def test_fmt_and_data_preserved_byte_exact(write_wav):
    src = write_wav("src.wav", np.sin(np.linspace(0, 20, 16000)).astype(np.float32))
    dst = src.replace("src.wav", "out.wav")

    cuewav.write_cues(src, dst, [{"position": 0.1, "label": "mark"}])

    src_fmt, src_data = _fmt_and_data(src)
    dst_fmt, dst_data = _fmt_and_data(dst)
    assert dst_fmt == src_fmt
    assert dst_data == src_data


def test_tamil_utf8_label_round_trips(write_wav):
    """The RIFF spec predates UTF-8 and 'labl' chunks carry no encoding tag -- this is the documented,
    hard-won property that a non-Latin label written as UTF-8 comes back byte-for-byte, not mangled or
    replaced with '?'."""
    src = write_wav("src.wav", np.zeros(16000, dtype=np.float32))
    dst = src.replace("src.wav", "out.wav")

    label = "அம்மா"
    report = cuewav.write_cues(src, dst, [{"position": 0.0, "label": label}], encoding="utf-8")
    assert report["fallback_or_lossy_count"] == 0

    read_back = cuewav.read_cues(dst)
    assert len(read_back) == 1
    assert read_back[0]["label"] == label
    assert read_back[0]["decoded_as"] == "utf-8"


def test_empty_cue_list(write_wav):
    src = write_wav("src.wav", np.zeros(1600, dtype=np.float32))
    dst = src.replace("src.wav", "out.wav")

    report = cuewav.write_cues(src, dst, [])
    assert report["num_cues"] == 0
    assert report["verified"] is True

    assert cuewav.read_cues(dst) == []
    # fmt/data must still be intact even with no cue chunk written
    src_fmt, src_data = _fmt_and_data(src)
    dst_fmt, dst_data = _fmt_and_data(dst)
    assert dst_fmt == src_fmt
    assert dst_data == src_data


def test_cues_at_start_and_end_of_file(write_wav):
    seconds = 1.0
    x = np.zeros(int(seconds * 16000), dtype=np.float32)
    src = write_wav("src.wav", x)
    dst = src.replace("src.wav", "out.wav")

    total_frames = len(x)
    cues = [
        {"position": 0, "label": "start"},
        {"position": total_frames, "label": "end"},   # exactly at the last sample offset
    ]
    report = cuewav.write_cues(src, dst, cues)
    assert report["verified"] is True

    read_back = cuewav.read_cues(dst)
    positions = {c["label"]: c["position_samples"] for c in read_back}
    assert positions["start"] == 0
    assert positions["end"] == total_frames


def test_position_past_end_is_clamped_not_rejected(write_wav):
    x = np.zeros(1600, dtype=np.float32)
    src = write_wav("src.wav", x)
    dst = src.replace("src.wav", "out.wav")

    report = cuewav.write_cues(src, dst, [{"position": 999.0, "label": "way past end"}])
    assert report["cues"][0]["position_samples"] == len(x)


def test_long_label_round_trips(write_wav):
    src = write_wav("src.wav", np.zeros(1600, dtype=np.float32))
    dst = src.replace("src.wav", "out.wav")

    long_label = "verse one, take four (the good one, not the one where the singer laughs) " * 5
    report = cuewav.write_cues(src, dst, [{"position": 0.0, "label": long_label}])
    assert report["verified"] is True

    read_back = cuewav.read_cues(dst)
    assert read_back[0]["label"] == long_label


def test_read_cues_returns_empty_for_wav_without_cue_chunk(write_wav):
    src = write_wav("src.wav", np.zeros(1600, dtype=np.float32))
    assert cuewav.read_cues(src) == []


def test_read_cues_raises_on_truncated_buffer(write_wav, tmp_path):
    """A corrupt/short buffer must raise, not silently return a (possibly wrong) cue list."""
    src = write_wav("src.wav", np.zeros(1600, dtype=np.float32))
    dst = src.replace("src.wav", "out.wav")
    cuewav.write_cues(src, dst, [{"position": 0.0, "label": "mark"}])

    with open(dst, "rb") as f:
        buf = f.read()

    truncated = tmp_path / "truncated.wav"
    # Chop off the tail -- still starts with a valid RIFF/WAVE header, but body is incomplete.
    truncated.write_bytes(buf[:20])
    with pytest.raises(ValueError):
        cuewav.read_cues(str(truncated))


def test_read_cues_raises_on_garbage_that_is_not_riff(tmp_path):
    garbage = tmp_path / "garbage.wav"
    garbage.write_bytes(b"not a riff file at all, just junk bytes" * 4)
    with pytest.raises(ValueError):
        cuewav.read_cues(str(garbage))


def test_write_cues_raises_on_malformed_source(tmp_path):
    bad_src = tmp_path / "bad.wav"
    bad_src.write_bytes(b"RIFF" + struct.pack("<I", 4) + b"WAVE")  # valid header, no fmt/data chunks
    dst = tmp_path / "out.wav"
    with pytest.raises(ValueError):
        cuewav.write_cues(str(bad_src), str(dst), [{"position": 0.0, "label": "x"}])


def test_write_cues_raises_on_unknown_encoding(write_wav):
    src = write_wav("src.wav", np.zeros(1600, dtype=np.float32))
    dst = src.replace("src.wav", "out.wav")
    with pytest.raises(ValueError):
        cuewav.write_cues(src, dst, [{"position": 0.0, "label": "x"}], encoding="nonsense")
