"""Pure attendance duration calculations, no CRM side effects."""
from __future__ import annotations
from datetime import datetime


def calculate_working_time(activities: list[dict], *, required_minutes: int = 540) -> dict:
    """Calculate closed intervals only. All timestamps must be aware datetimes.

    Open sessions are flagged and not treated as completed work. Breaks outside
    sessions do not reduce totals. Absence deductions require separately approved
    payroll semantics; they are NOT silently subtracted here.
    """
    if required_minutes < 1:
        raise ValueError("required_minutes must be positive")
    ordered = sorted(activities, key=lambda x: x["occurred_at"])
    active = None
    break_start = None
    gross_seconds = 0.0
    break_seconds = 0.0
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
            break_seconds += max(0.0, (at-break_start).total_seconds())
            break_start = None
        elif action == "CHECK_OUT" and active is not None:
            gross_seconds += max(0.0, (at-active).total_seconds())
            if break_start is not None:
                break_seconds += max(0.0, (at-break_start).total_seconds())
            active, break_start = None, None
    gross = int(gross_seconds // 60)
    breaks = min(gross, int(break_seconds // 60))
    net = max(0, gross-breaks)
    return {
        "grossMinutes": gross, "breakMinutes": breaks, "netMinutes": net,
        "requiredMinutes": required_minutes, "shortfallMinutes": max(0, required_minutes-net),
        "overtimeMinutes": max(0, net-required_minutes),
        "hasOpenSession": active is not None,
        "calculationStatus": "PROVISIONAL" if active is not None else "CLOSED_SESSIONS_ONLY",
        "absenceDeductionApplied": False,
    }
