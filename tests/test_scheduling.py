from datetime import timedelta

import pytest

from open_index.scheduling import RunState, is_due, parse_schedule, utcnow


def test_parse_schedule_forms():
    assert parse_schedule("manual") is None
    assert parse_schedule("hourly") == 3600
    assert parse_schedule("daily") == 86400
    assert parse_schedule("6h") == 6 * 3600
    assert parse_schedule("30m") == 1800
    assert parse_schedule("2w") == 2 * 604800
    assert parse_schedule(120) == 120


def test_parse_schedule_bad():
    with pytest.raises(ValueError):
        parse_schedule("banana")


def test_is_due_never_run_is_due():
    assert is_due("daily", None, utcnow()) is True


def test_is_due_manual_never():
    assert is_due("manual", None, utcnow()) is False


def test_is_due_respects_interval():
    now = utcnow()
    recent = (now - timedelta(hours=1)).isoformat()
    old = (now - timedelta(hours=25)).isoformat()
    assert is_due("daily", recent, now) is False
    assert is_due("daily", old, now) is True


def test_run_state_round_trip(tmp_path):
    state = RunState(tmp_path)
    assert state.last_run("c") is None
    now = utcnow()
    state.record("c", now, "ok", 5)
    # reload from disk
    reloaded = RunState(tmp_path)
    assert reloaded.last_run("c") == now.isoformat()
