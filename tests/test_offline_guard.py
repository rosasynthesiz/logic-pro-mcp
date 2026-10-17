"""logic_offline(in_place=True) must fail closed: it patches the original ProjectData only when it is PROVEN
that Logic does not have the project open. Synthetic bundles only; no .logicx of anyone's is read."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from logic_mcp import server
from tests import fakeax


def _bundle(tmp_path, name="Fixture Song.logicx") -> Path:
    b = tmp_path / name
    (b / "Alternatives" / "000").mkdir(parents=True)
    (b / "Alternatives" / "000" / "ProjectData").write_bytes(b"\x00synthetic\x00")
    return b


@pytest.fixture
def offline(monkeypatch, tmp_path):
    """A rename that would succeed on the bytes, so only the in_place gate decides the outcome."""
    monkeypatch.setattr(server.projectdata, "rename_many", lambda data, changes: data + b"!")
    monkeypatch.setattr(server.projectdata, "records", lambda data: [{"kind": "track", "name": "New"}])
    monkeypatch.setattr(server.projectdata, "check_layout", lambda data: [])
    return _bundle(tmp_path)


def _untouched(bundle: Path) -> bool:
    pdata = bundle / "Alternatives" / "000" / "ProjectData"
    return pdata.read_bytes() == b"\x00synthetic\x00" and not list(pdata.parent.glob("ProjectData.bak*"))


def test_logic_not_running_is_proof_enough(monkeypatch, offline):
    L = fakeax.wire(monkeypatch)
    L.target.find.return_value = fakeax.logic_app(pid=None)
    monkeypatch.setattr(server, "_app", lambda: fakeax.logic_app(pid=None))
    assert server._offline_in_place_blocker(offline) is None
    r = server.logic_offline("rename", project=str(offline), changes=[{"old": "Old", "new": "New"}], in_place=True)
    assert r["state"] == "uncertain" and r["readback"]["bundle"] == str(offline)
    assert (offline / "Alternatives" / "000" / "ProjectData").read_bytes().endswith(b"!")


def test_running_without_accessibility_cannot_prove_closed(monkeypatch, offline):
    L = fakeax.wire(monkeypatch)
    L.ax._trusted = False
    r = server.logic_offline("rename", project=str(offline), changes=[{"old": "Old", "new": "New"}], in_place=True)
    assert r["state"] == "failed" and "cannot be proven" in r["reason"]
    assert _untouched(offline)


def test_locked_screen_cannot_prove_closed(monkeypatch, offline):
    fakeax.wire(monkeypatch)
    monkeypatch.setattr(server.doctor, "session_state", lambda: {"locked": True, "display_asleep": False, "ok": False})
    r = server.logic_offline("rename", project=str(offline), changes=[{"old": "Old", "new": "New"}], in_place=True)
    assert r["state"] == "failed" and "locked" in r["reason"]
    assert _untouched(offline)


def test_running_with_no_visible_window_cannot_prove_closed(monkeypatch, offline):
    L = fakeax.wire(monkeypatch)
    L.root.children.clear()                                  # Logic is up, AX shows no window at all
    r = server.logic_offline("rename", project=str(offline), changes=[{"old": "Old", "new": "New"}], in_place=True)
    assert r["state"] == "failed" and "no window" in r["reason"]
    assert _untouched(offline)


def test_open_project_by_document_url_is_refused(monkeypatch, offline):
    fakeax.wire(monkeypatch, doc=offline.as_uri(), title="Fixture Song - Tracks")
    r = server.logic_offline("rename", project=str(offline), changes=[{"old": "Old", "new": "New"}], in_place=True)
    assert r["state"] == "failed" and "has this project open" in r["reason"]
    assert _untouched(offline)


def test_open_project_by_window_title_is_refused(monkeypatch, offline):
    fakeax.wire(monkeypatch, doc="", title="Fixture Song - Tracks")
    r = server.logic_offline("rename", project=str(offline), changes=[{"old": "Old", "new": "New"}], in_place=True)
    assert r["state"] == "failed" and "looks like this project" in r["reason"]
    assert _untouched(offline)


def test_substring_of_another_project_name_is_not_a_match(monkeypatch, offline, tmp_path):
    """'Song.logicx' must not be mistaken for open because 'My Song.logicx' is -- and vice versa."""
    other = _bundle(tmp_path, "Song.logicx")
    fakeax.wire(monkeypatch, doc=offline.as_uri(), title="Fixture Song - Tracks")   # Fixture Song is open
    assert server._offline_in_place_blocker(other) is None


def test_other_project_open_lets_the_patch_proceed(monkeypatch, offline):
    fakeax.wire(monkeypatch, doc="file:///tmp/Another.logicx", title="Another - Tracks")
    r = server.logic_offline("rename", project=str(offline), changes=[{"old": "Old", "new": "New"}], in_place=True)
    assert r["state"] == "uncertain"
    assert list((offline / "Alternatives" / "000").glob("ProjectData.bak*"))
