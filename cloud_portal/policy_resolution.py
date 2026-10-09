"""Deterministic field-level attendance policy inheritance.

Precedence: explicit user value > explicit shop value > system default.
A false boolean and zero are intentional values, not missing values.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Any, Mapping

SYSTEM_DEFAULTS: dict[str, Any] = {
    "gracePeriodMinutes": 15,
    "allowedBreakMinutes": 60,
    "requiredWorkingMinutes": 480,
    "maxLogoffTime": "21:30",
    "absenceAutoLogoutEnabled": True,
    "absenceMonitoringEnabled": True,
    "outOfCameraGraceMinutes": 5,
    "adminNotificationAfterMinutes": 15,
    "markAbsentAfterMinutes": 60,
    "maxOutOfCameraOccurrencesPerDay": 5,
    "presenceUpdateIntervalMinutes": 2,
    "attendanceMode": "AUTO",
    "dayEndAutoLogoutEnabled": True,
    "timezone": "Asia/Kolkata",
}
SHOP_ALIASES = {
    "gracePeriodMinutes": "grace_period_minutes",
    "allowedBreakMinutes": "allowed_break_minutes",
    "requiredWorkingMinutes": "total_working_minutes",
    "maxLogoffTime": "max_logoff_time",
    "absenceAutoLogoutEnabled": "absence_auto_logout_enabled",
}
# Shop grace maps to general grace; the V2 out-of-camera threshold is
# independent unless a shop-specific field is explicitly provided.
USER_ALIASES = {
    "requiredWorkingMinutes": "requiredWorkingMinutes",
    "outOfCameraGraceMinutes": "outOfCameraGraceMinutes",
}

@dataclass(frozen=True)
class ResolvedAttendancePolicy:
    values: dict[str, Any]
    sources: dict[str, str]

def resolve_attendance_policy(
    user_policy: Mapping[str, Any] | None,
    shop_policy: Mapping[str, Any] | None,
    defaults: Mapping[str, Any] | None = None,
) -> ResolvedAttendancePolicy:
    user, shop = user_policy or {}, shop_policy or {}
    base = dict(SYSTEM_DEFAULTS if defaults is None else defaults)
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for field, default in base.items():
        user_key = USER_ALIASES.get(field, field)
        shop_key = SHOP_ALIASES.get(field, field)
        if user_key in user and user[user_key] is not None:
            values[field], sources[field] = user[user_key], "USER"
        elif shop_key in shop and shop[shop_key] is not None:
            values[field], sources[field] = shop[shop_key], "SHOP"
        else:
            values[field], sources[field] = default, "SYSTEM"
    return ResolvedAttendancePolicy(values, sources)
