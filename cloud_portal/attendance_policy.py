from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo


@dataclass(frozen=True)
class AttendancePolicy:
    grace_period_minutes: int = 15
    allowed_break_minutes: int = 60
    total_working_minutes: int = 480
    max_logoff_time: str = "21:30"
    absence_auto_logout_enabled: bool = True
    timezone: str = "Asia/Kolkata"

    @classmethod
    def from_mapping(cls, value: dict) -> "AttendancePolicy":
        return cls(
            grace_period_minutes=max(1, int(value.get("grace_period_minutes", 15))),
            allowed_break_minutes=max(0, int(value.get("allowed_break_minutes", 60))),
            total_working_minutes=max(1, int(value.get("total_working_minutes", 480))),
            max_logoff_time=str(value.get("max_logoff_time") or "21:30"),
            absence_auto_logout_enabled=bool(value.get("absence_auto_logout_enabled", True)),
            timezone=str(value.get("timezone") or "Asia/Kolkata"),
        )

    def absence_deadline(self, last_seen_at: datetime) -> datetime:
        return last_seen_at + timedelta(minutes=self.grace_period_minutes)

    def max_logoff_deadline(self, day: datetime) -> datetime:
        zone = ZoneInfo(self.timezone)
        local = day.astimezone(zone)
        hh, mm = (int(part) for part in self.max_logoff_time.split(":", 1))
        return datetime.combine(local.date(), time(hh, mm), tzinfo=zone)

    def should_auto_logout_for_absence(self, *, now: datetime, last_seen_at: datetime,
                                       checked_in: bool, on_break: bool,
                                       camera_coverage_healthy: bool) -> bool:
        if not self.absence_auto_logout_enabled or not checked_in or on_break or not camera_coverage_healthy:
            return False
        return now >= self.absence_deadline(last_seen_at)

    def daily_status(self, worked_minutes: int) -> str:
        return "COMPLETE" if worked_minutes >= self.total_working_minutes else "SHORT_HOURS"
