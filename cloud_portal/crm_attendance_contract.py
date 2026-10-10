"""Pure formatting helpers for the confirmed SnapKey LoginLogout contract."""

from datetime import datetime
from zoneinfo import ZoneInfo


def format_crm_attendance_date_time(event_time: datetime, timezone_name: str) -> tuple[str,str]:
    if event_time.tzinfo is None:
        raise ValueError("CRM attendance event time must be timezone-aware")
    if not timezone_name:
        raise ValueError("CRM attendance timezone is required")
    local_time=event_time.astimezone(ZoneInfo(timezone_name))
    return local_time.isoformat(timespec="milliseconds"),local_time.strftime("%H:%M:%S")
