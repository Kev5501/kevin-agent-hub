from __future__ import annotations

from datetime import UTC, datetime

from core.schedule import is_due, next_fire, previous_fire

WEEKLY_MON_NOON = "0 12 * * 1"


def at(y, m, d, h=0, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=UTC)


def test_due_on_the_day_after_the_fire_time_within_window():
    # 2024-01-01 is a Monday. Dispatcher at 13:23 -> 1h23m after the 12:00 fire.
    assert is_due(WEEKLY_MON_NOON, at(2024, 1, 1, 13, 23), "UTC", 24)


def test_not_due_the_next_day_or_midweek():
    assert not is_due(WEEKLY_MON_NOON, at(2024, 1, 2, 13, 23), "UTC", 24)
    assert not is_due(WEEKLY_MON_NOON, at(2024, 1, 5, 13, 23), "UTC", 24)


def test_tolerates_late_dispatcher_start():
    assert is_due(WEEKLY_MON_NOON, at(2024, 1, 1, 20, 0), "UTC", 24)


def test_exact_fire_time_counts_as_due():
    assert is_due(WEEKLY_MON_NOON, at(2024, 1, 1, 12, 0), "UTC", 24)


def test_daily_and_monthly_agents():
    assert is_due("0 6 * * *", at(2024, 3, 9, 13, 23), "UTC", 24)
    assert is_due("0 6 1 * *", at(2024, 3, 1, 13, 23), "UTC", 24)
    assert not is_due("0 6 1 * *", at(2024, 3, 2, 13, 23), "UTC", 24)


def test_timezone_is_respected():
    # 08:00 America/Toronto in January == 13:00 UTC
    assert is_due("0 8 * * *", at(2024, 1, 10, 13, 23), "America/Toronto", 24)
    assert previous_fire("0 8 * * *", at(2024, 1, 10, 13, 23), "America/Toronto").hour == 8


def test_next_fire():
    assert next_fire(WEEKLY_MON_NOON, at(2024, 1, 1, 13, 0), "UTC") == at(2024, 1, 8, 12, 0)
