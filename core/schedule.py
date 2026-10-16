"""Per-agent scheduling.

GitHub's own cron is fixed in the workflow file, so the workflow fires once a day (the
"dispatcher") and each agent carries its own cron expression. An agent is *due* when its most
recent scheduled fire time falls within the last `window_hours` (= the dispatcher interval).
That tolerates GitHub starting runs late; agents are idempotent so an occasional double run is
harmless.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from croniter import croniter


def validate_cron(expr: str) -> None:
    if not isinstance(expr, str) or not croniter.is_valid(expr):
        raise ValueError(f"Invalid cron expression: {expr!r}")


def previous_fire(expr: str, now: datetime, tz: str = "UTC") -> datetime:
    """Most recent scheduled time at or before `now` (inclusive)."""
    local = now.astimezone(ZoneInfo(tz))
    return croniter(expr, local + timedelta(seconds=1)).get_prev(datetime)


def next_fire(expr: str, now: datetime, tz: str = "UTC") -> datetime:
    local = now.astimezone(ZoneInfo(tz))
    return croniter(expr, local).get_next(datetime)


def is_due(expr: str, now: datetime, tz: str = "UTC", window_hours: float = 24) -> bool:
    prev = previous_fire(expr, now, tz)
    age = now - prev
    return timedelta(0) <= age <= timedelta(hours=window_hours)
