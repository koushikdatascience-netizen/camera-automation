"""Pure formatting helpers for the configurable SnapKey attendance date contract."""

from datetime import datetime
from zoneinfo import ZoneInfo


def format_crm_attendance_date_time(event_time: datetime, timezone_name: str,
                                    date_format: str) -> tuple[str,str]:
    if event_time.tzinfo is None:
        raise ValueError("CRM attendance event time must be timezone-aware")
    if not timezone_name:
        raise ValueError("CRM attendance timezone is required")
    if not date_format:
        raise ValueError("CRM attendance date format is required")
    local_time=event_time.astimezone(ZoneInfo(timezone_name))
    return local_time.strftime(date_format),local_time.strftime("%H:%M:%S")
