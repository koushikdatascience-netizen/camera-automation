"""Pure, timezone-aware attendance duration calculations."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo


def calculate_working_time(
    activities: list[dict], *, required_minutes: int = 540,
    day: date | None = None, timezone_name: str = "UTC",
    as_of: datetime | None = None,
) -> dict:
    """Calculate work and breaks clipped to a local business day.

    When ``day`` is provided, overnight sessions are clipped at local midnight
    and an open session is provisionally counted through ``as_of`` (or now).
    With no day, legacy closed-session behavior is preserved.
    """
    if required_minutes < 1:
        raise ValueError("required_minutes must be positive")
    zone = ZoneInfo(timezone_name)
    cutoff = as_of or datetime.now(timezone.utc)
    if cutoff.tzinfo is None:
        raise ValueError("timezone-aware as_of required")
    if day is not None:
        window_start = datetime.combine(day, time.min, tzinfo=zone)
        window_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
        window_start = window_start.astimezone(timezone.utc)
        window_end = window_end.astimezone(timezone.utc)
    else:
        window_start = window_end = None

    ordered = sorted(activities, key=lambda x: x["occurred_at"])
    active: datetime | None = None
    break_start: datetime | None = None
    gross_seconds = 0.0
    break_seconds = 0.0
    has_open_session = False

    def clipped_seconds(start: datetime, end: datetime) -> float:
        if day is None:
            return max(0.0, (end - start).total_seconds())
        left = max(start, window_start)
        right = min(end, window_end, cutoff.astimezone(timezone.utc))
        return max(0.0, (right - left).total_seconds())

    for item in ordered:
        at = item["occurred_at"]
        if not isinstance(at, datetime) or at.tzinfo is None:
            raise ValueError("timezone-aware occurred_at required")
        action = item["activity_type"]
        if action == "CHECK_IN" and active is None:
            active, break_start = at, None
        elif action == "BREAK_START" and active is not None and break_start is None:
            break_start = at
        elif action == "BREAK_END" and break_start is not None:
            break_seconds += clipped_seconds(break_start, at)
            break_start = None
        elif action == "CHECK_OUT" and active is not None:
            gross_seconds += clipped_seconds(active, at)
            if break_start is not None:
                break_seconds += clipped_seconds(break_start, at)
            active, break_start = None, None

    if active is not None and day is not None:
        end = min(cutoff.astimezone(timezone.utc), window_end)
        gross_seconds += clipped_seconds(active, end)
        if break_start is not None:
            break_seconds += clipped_seconds(break_start, end)
        has_open_session = True

    gross = int(gross_seconds // 60)
    breaks = min(gross, int(break_seconds // 60))
    net = max(0, gross - breaks)
    return {
        "grossMinutes": gross, "breakMinutes": breaks, "netMinutes": net,
        "requiredMinutes": required_minutes, "shortfallMinutes": max(0, required_minutes-net),
        "overtimeMinutes": max(0, net-required_minutes),
        "hasOpenSession": active is not None,
        "calculationStatus": "PROVISIONAL" if active is not None else "CLOSED_SESSIONS_ONLY",
        "absenceDeductionApplied": False,
    }
