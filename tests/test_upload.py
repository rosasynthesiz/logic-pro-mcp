"""Whose cloud account the audio goes to.

Development ran against one personal Google Drive remote. Shipping that default would have uploaded every
user's unreleased audio into someone else's Drive, so the rule is now: no bundled credentials, no default
account, and no guessing between several configured remotes. These tests pin that, because the failure is
silent, remote, and not undoable.

Nothing here runs rclone or touches the network — `remotes()` is monkeypatched.
"""
from __future__ import annotations

import pytest

from logic_mcp import upload


@pytest.fixture(autouse=True)
def no_env(monkeypatch):
    monkeypatch.delenv(upload.REMOTE_ENV, raising=False)


@pytest.fixture
def configured(monkeypatch):
    def _set(*names):
        monkeypatch.setattr(upload, "remotes", lambda: list(names))
    return _set


# ---------------------------------------------------------------------------
# no default account
# ---------------------------------------------------------------------------

def test_there_is_no_baked_in_remote():
    """If this ever becomes a string again, every user's bounces go to whoever's account it names."""
    assert upload.DEFAULT_REMOTE is None


def test_no_remote_configured_is_an_error_not_a_guess(configured):
    configured()
    remote, err = upload.resolve_remote(None)
    assert remote is None and err


def test_single_remote_is_selected(configured):
    configured("mydrive")
    assert upload.resolve_remote(None) == ("mydrive", None)


def test_several_remotes_refuse_to_guess(configured):
    """Picking wrong here publishes a client's unreleased audio to the wrong account."""
    configured("work", "personal")
    remote, err = upload.resolve_remote(None)
    assert remote is None
    assert "work" in err and "personal" in err, "the error must name the choices"


def test_explicit_remote_wins(configured):
    configured("work", "personal")
    assert upload.resolve_remote("personal") == ("personal", None)


def test_explicit_unknown_remote_is_refused(configured):
    configured("work")
    remote, err = upload.resolve_remote("typo")
    assert remote is None and "typo" in err


def test_env_var_pins_the_remote(configured, monkeypatch):
    configured("work", "personal")
    monkeypatch.setenv(upload.REMOTE_ENV, "personal")
    assert upload.resolve_remote(None) == ("personal", None)


def test_argument_beats_the_env_var(configured, monkeypatch):
    configured("work", "personal")
    monkeypatch.setenv(upload.REMOTE_ENV, "personal")
    assert upload.resolve_remote("work") == ("work", None)


def test_env_var_naming_a_missing_remote_is_refused(configured, monkeypatch):
    configured("work")
    monkeypatch.setenv(upload.REMOTE_ENV, "ghost")
    remote, err = upload.resolve_remote(None)
    assert remote is None and "ghost" in err


def test_blank_env_var_is_ignored(configured, monkeypatch):
    configured("only")
    monkeypatch.setenv(upload.REMOTE_ENV, "   ")
    assert upload.resolve_remote(None) == ("only", None)


# ---------------------------------------------------------------------------
# setup guidance
# ---------------------------------------------------------------------------

def test_setup_hint_tells_the_user_what_to_run():
    hint = upload.setup_hint("mydrive")
    assert "rclone config create mydrive" in hint["connect"]
    assert "brew install rclone" in hint["install"]


def test_setup_hint_carries_no_secrets():
    """The hint is shown to a model and logged, so it must carry no credential VALUE.

    Naming a credential in prose ("rclone stores the token in your own config") is fine and useful; what
    must never appear is an assignment that hands one over, so the check is assignment-shaped rather than
    a bare word search.
    """
    import re
    blob = " ".join(str(v) for v in upload.setup_hint().values())
    leak = re.search(r"(client_secret|client_id|refresh_token|api_key|password|token)\s*[=:]\s*\S+",
                     blob, re.IGNORECASE)
    assert leak is None, f"setup guidance hands over a credential: {leak.group(0)!r}"


def test_setup_hint_contains_no_long_opaque_strings():
    """A pasted key would show up as a long unbroken run of credential-ish characters."""
    import re
    for value in upload.setup_hint().values():
        for word in str(value).split():
            assert not re.fullmatch(r"[A-Za-z0-9_\-]{40,}", word), f"suspicious opaque string: {word!r}"


def test_setup_hint_does_not_offer_to_authenticate_for_the_user():
    """Authorisation happens in the user's own browser under their own account; this server must only
    ever hand over the command, never run it."""
    hint = upload.setup_hint()
    assert "your own" in hint["note"].lower()


# ---------------------------------------------------------------------------
# upload refusals
# ---------------------------------------------------------------------------

def test_upload_refuses_without_a_resolvable_remote(configured, tmp_path):
    configured("a", "b")
    f = tmp_path / "x.wav"
    f.write_bytes(b"RIFF")
    res = upload.upload([str(f)])
    assert res["state"] == "failed"
    assert res["fix"], "a refusal must come with the fix"


def test_upload_refuses_missing_local_files(configured, monkeypatch):
    configured("only")
    monkeypatch.setattr(upload, "available", lambda: True)
    res = upload.upload(["/definitely/not/here.wav"], remote="only")
    assert res["state"] == "failed"
    assert "missing" in res["reason"]


def test_upload_reports_failure_when_rclone_is_absent(monkeypatch):
    monkeypatch.setattr(upload, "available", lambda: False)
    res = upload.upload(["/tmp/whatever.wav"])
    assert res["state"] == "failed"
    assert res["fix"]["install"]
