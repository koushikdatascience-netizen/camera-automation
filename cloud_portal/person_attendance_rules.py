"""Person-wise attendance rule evaluation with no direct CRM side effects.

The cloud worker persists and acts on emitted transitions. Every transition must
be deduplicated by (episode_id, kind) before any external action is attempted.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class PersonAttendancePolicy:
    presence_update_interval_minutes: int = 2
    out_of_camera_grace_minutes: int = 5
    max_out_of_camera_occurrences_per_day: int = 5
    admin_notification_after_minutes: int = 15
    mark_absent_after_minutes: int = 60
    required_working_minutes: int = 540
    attendance_mode: str = "AUTO"
    timezone: str = "Asia/Kolkata"

    def __post_init__(self) -> None:
        if self.attendance_mode not in {"AUTO", "MANUAL"}:
            raise ValueError("attendance_mode must be AUTO or MANUAL")
        if not 1 <= self.presence_update_interval_minutes <= 60:
            raise ValueError("invalid presence update interval")
        if not 1 <= self.out_of_camera_grace_minutes < self.admin_notification_after_minutes < self.mark_absent_after_minutes <= 1440:
            raise ValueError("thresholds must satisfy grace < notify < absent")
        if not 1 <= self.max_out_of_camera_occurrences_per_day <= 100:
            raise ValueError("invalid daily occurrence limit")
        if not 1 <= self.required_working_minutes <= 1440:
            raise ValueError("invalid working target")
        ZoneInfo(self.timezone)


@dataclass(frozen=True)
class AbsenceEvaluation:
    state: str
    elapsed_minutes: float
    business_date: str
    transitions: tuple[str, ...]


def evaluate_absence(
    policy: PersonAttendancePolicy,
    *,
    now: datetime,
    last_seen_at: datetime,
    checked_in: bool,
    on_break: bool,
    camera_coverage_healthy: bool,
    completed_episodes_today: int,
    active_episode_counted: bool = False,
) -> AbsenceEvaluation:
    """Return desired state and transitions; caller owns persistence/idempotency.

    A count of 5 is allowed; the sixth qualifying episode breaches the limit.
    An unhealthy camera is UNKNOWN, never evidence of absence.
    """
    if now.tzinfo is None or last_seen_at.tzinfo is None:
        raise ValueError("timezone-aware datetimes required")
    if completed_episodes_today < 0:
        raise ValueError("episode count cannot be negative")
    elapsed = max(0.0, (now - last_seen_at).total_seconds() / 60)
    day = now.astimezone(ZoneInfo(policy.timezone)).date().isoformat()
    if not checked_in:
        return AbsenceEvaluation("NOT_CHECKED_IN", elapsed, day, ())
    if on_break:
        return AbsenceEvaluation("ON_BREAK", elapsed, day, ())
    if not camera_coverage_healthy:
        return AbsenceEvaluation("UNKNOWN_CAMERA_UNHEALTHY", elapsed, day, ())
    if elapsed < policy.out_of_camera_grace_minutes:
        return AbsenceEvaluation("PRESENT_OR_GRACE", elapsed, day, ())
    events = ["GRACE_EXCEEDED"]
    occurrences = completed_episodes_today + (0 if active_episode_counted else 1)
    if occurrences > policy.max_out_of_camera_occurrences_per_day:
        events.append("DAILY_ABSENCE_LIMIT_EXCEEDED")
    if elapsed >= policy.admin_notification_after_minutes:
        events.append("ADMIN_ABSENCE_WARNING")
    if elapsed >= policy.mark_absent_after_minutes:
        events.append("PROLONGED_ABSENCE")
        events.append("CRM_ABSENT_ACTION_PENDING")
        state = "PROLONGED_ABSENCE"
    elif elapsed >= policy.admin_notification_after_minutes:
        state = "ADMIN_WARNING"
    else:
        state = "OUT_OF_CAMERA"
    return AbsenceEvaluation(state, elapsed, day, tuple(events))
