"""The Claude plugin vendors logic_mcp; fail when the copy drifts from the source."""
import importlib.util
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "sync_claude_plugin", Path(__file__).resolve().parent.parent / "scripts" / "sync_claude_plugin.py")
sync = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sync)


def test_vendored_copy_matches_source():
    assert sync.drift() == [], "run `python scripts/sync_claude_plugin.py` and commit the result"


def test_plugin_has_no_stray_files():
    assert not [p for p in sync.DST.rglob("__pycache__")], "remove __pycache__ from the plugin copy"
