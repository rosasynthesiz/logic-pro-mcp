"""AppleScript string quoting. `osascript -e` runs whatever text it is handed, so every value that
goes inside a string literal has to be escaped, or a file name ends the literal and the rest runs."""
from __future__ import annotations

from logic_mcp.channels import applescript


def _capture(monkeypatch):
    seen = []
    monkeypatch.setattr(applescript, "run", lambda script, timeout=10.0: (seen.append(script) or (True, "")))
    return seen


def test_open_file_escapes_quotes_and_backslashes(monkeypatch):
    seen = _capture(monkeypatch)
    applescript.open_file("Logic Pro", '/tmp/Mix "final" \\ v2.logicx')
    assert seen == ['tell application "Logic Pro" to open POSIX file "/tmp/Mix \\"final\\" \\\\ v2.logicx"']


def test_a_hostile_name_cannot_leave_the_string_literal(monkeypatch):
    seen = _capture(monkeypatch)
    applescript.open_file("Logic Pro", '" & (do shell script "echo pwned") & "')
    script = seen[0]
    # Every double quote the caller supplied is escaped; the only bare quotes are the four we wrote.
    bare = [i for i, ch in enumerate(script) if ch == '"' and script[i - 1] != "\\"]
    assert len(bare) == 4, script


def test_menu_path_and_app_name_are_quoted_too(monkeypatch):
    seen = _capture(monkeypatch)
    applescript.press_menu('Logic "Pro"', "Track", 'New "Audio" Track')
    assert 'process "Logic \\"Pro\\""' in seen[0]
    assert 'menu item "New \\"Audio\\" Track"' in seen[0]
    for fn in (applescript.activate, applescript.quit_app, applescript.is_running):
        seen.clear()
        fn('Logic "Pro"')
        assert '\\"Pro\\"' in seen[0]
