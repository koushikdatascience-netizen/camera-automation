"""Validation and field-level normalization for cloud-to-edge policy snapshots."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from cloud_portal.policy_resolution import SHOP_ALIASES, USER_ALIASES


POLICY_ALIASES = {
    "attendance_mode": "attendanceMode",
    "grace_period_minutes": "gracePeriodMinutes",
    "allowed_break_minutes": "allowedBreakMinutes",
    "total_working_minutes": "requiredWorkingMinutes",
    "max_logoff_time": "maxLogoffTime",
    "absence_auto_logout_enabled": "absenceAutoLogoutEnabled",
    "absence_monitoring_enabled": "absenceMonitoringEnabled",
    "mark_absent_after_minutes": "markAbsentAfterMinutes",
    "presence_update_interval_minutes": "presenceUpdateIntervalMinutes",
    "out_of_camera_grace_minutes": "outOfCameraGraceMinutes",
    "max_out_of_camera_occurrences_per_day": "maxOutOfCameraOccurrencesPerDay",
    "admin_notification_after_minutes": "adminNotificationAfterMinutes",
    "day_end_auto_logout_enabled": "dayEndAutoLogoutEnabled",
    "attendance_day_start_time": "attendanceDayStartTime",
    "shift_start_time": "shiftStartTime",
    "late_grace_minutes": "lateGraceMinutes",
    "scheduled_weekdays": "scheduledWeekdays",
    "overtime_enabled": "overtimeEnabled",
    "email_recipients": "emailRecipients",
    "whatsapp_recipients": "whatsappRecipients",
}


def normalize_effective_policy(user_policy: dict, shop_policy: dict) -> dict:
    """Return a complete snake-case WorkspacePolicy using user > shop > defaults."""
    from camera_service.attendance_workspace_api import WorkspacePolicy

    result = WorkspacePolicy().model_dump()
    for field, default in tuple(result.items()):
        alias = POLICY_ALIASES.get(field)
        user_keys = (field, alias) if alias else (field,)
        shop_alias = SHOP_ALIASES.get(alias, alias) if alias else None
        shop_keys = tuple(dict.fromkeys(k for k in (field, shop_alias, alias) if k))
        selected = next((user_policy[k] for k in user_keys
                         if k in user_policy and user_policy[k] is not None), None)
        if selected is None:
            selected = next((shop_policy[k] for k in shop_keys
                             if k in shop_policy and shop_policy[k] is not None), default)
        result[field] = selected
    return validate_complete_policy(result)


def validate_complete_policy(value: object) -> dict:
    """Reject partial, malformed or semantically unsafe snapshots before persistence."""
    from camera_service.attendance_workspace_api import WorkspacePolicy

    if not isinstance(value, dict):
        raise ValueError("Attendance policy must be an object")
    expected = set(WorkspacePolicy.model_fields)
    if set(value) != expected:
        raise ValueError("Attendance policy fields are incomplete or unexpected")
    try:
        normalized = WorkspacePolicy.model_validate(value).model_dump()
        if not normalized["out_of_camera_grace_minutes"] < normalized["admin_notification_after_minutes"] < normalized["mark_absent_after_minutes"]:
            raise ValueError("Attendance policy thresholds are inconsistent")
        ZoneInfo(normalized["timezone"])
    except (ValidationError, KeyError, TypeError) as exc:
        raise ValueError("Attendance policy is invalid") from exc
    return normalized


def validate_policy_timestamp(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("Attendance policy timestamp is required")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Attendance policy timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Attendance policy timestamp must include a timezone")
    return parsed.isoformat()
