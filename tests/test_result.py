"""Pins down logic_mcp.result: the honest result envelope every mutating tool returns.

The whole project's trust model rests on agents only ever treating `state == "confirmed"` as "it
happened". If the CONFIRMED/UNCERTAIN/FAILED literals drift, or ok()/unsure()/fail() stop carrying the
fields callers key off, that trust model silently breaks everywhere at once -- hence the very literal
assertions below.
"""
from __future__ import annotations

from logic_mcp import result


def test_state_literals_are_exactly_these_strings():
    # The whole project's contract depends on these exact strings (e.g. callers doing
    # `if res["state"] == "confirmed"`), so pin the literals themselves, not just the constant names.
    assert result.CONFIRMED == "confirmed"
    assert result.UNCERTAIN == "uncertain"
    assert result.FAILED == "failed"


def test_the_three_states_are_distinct():
    assert len({result.CONFIRMED, result.UNCERTAIN, result.FAILED}) == 3


def test_ok_produces_confirmed_state_with_channel():
    res = result.ok("transport", detail="started playback")
    assert res["state"] == result.CONFIRMED
    assert res["channel"] == "transport"
    assert res["detail"] == "started playback"
    assert res["readback"] is None


def test_ok_carries_readback_and_extra_kwargs():
    res = result.ok("tracks", detail="renamed", readback={"name": "Vocal Comp"}, track_id=3)
    assert res["state"] == result.CONFIRMED
    assert res["readback"] == {"name": "Vocal Comp"}
    assert res["track_id"] == 3


def test_unsure_produces_uncertain_state_with_reason():
    res = result.unsure("osc", reason="no ack within timeout")
    assert res["state"] == result.UNCERTAIN
    assert res["channel"] == "osc"
    assert res["reason"] == "no ack within timeout"
    assert res["readback"] is None


def test_unsure_carries_readback_and_extra_kwargs():
    res = result.unsure("mix", "value drifted", readback={"gain_db": -3.2}, attempts=2)
    assert res["state"] == result.UNCERTAIN
    assert res["readback"] == {"gain_db": -3.2}
    assert res["attempts"] == 2


def test_fail_produces_failed_state_with_reason_and_fix():
    res = result.fail("plugins", reason="channel not ready", fix="grant Accessibility, restart Logic")
    assert res["state"] == result.FAILED
    assert res["channel"] == "plugins"
    assert res["reason"] == "channel not ready"
    assert res["fix"] == "grant Accessibility, restart Logic"


def test_fail_fix_is_optional_and_defaults_to_none():
    res = result.fail("plugins", reason="channel not ready")
    assert res["state"] == result.FAILED
    assert res["fix"] is None


def test_fail_carries_extra_kwargs():
    res = result.fail("export", "bounce failed", fix=None, exit_code=1)
    assert res["exit_code"] == 1


def test_every_result_carries_state_and_channel():
    for res in (
        result.ok("chan_a"),
        result.unsure("chan_b", "reason"),
        result.fail("chan_c", "reason"),
    ):
        assert "state" in res
        assert "channel" in res
        assert res["channel"] in ("chan_a", "chan_b", "chan_c")
