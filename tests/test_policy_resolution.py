from cloud_portal.policy_resolution import resolve_attendance_policy


def test_user_overrides_shop_field_by_field():
    result = resolve_attendance_policy(
        {"requiredWorkingMinutes": 420, "absenceAutoLogoutEnabled": False},
        {"total_working_minutes": 480, "absence_auto_logout_enabled": True,
         "grace_period_minutes": 15},
    )
    assert result.values["requiredWorkingMinutes"] == 420
    assert result.sources["requiredWorkingMinutes"] == "USER"
    assert result.values["absenceAutoLogoutEnabled"] is False
    assert result.sources["absenceAutoLogoutEnabled"] == "USER"
    assert result.values["gracePeriodMinutes"] == 15
    assert result.sources["gracePeriodMinutes"] == "SHOP"


def test_shop_overrides_system_when_user_missing():
    result = resolve_attendance_policy({}, {"total_working_minutes": 510})
    assert result.values["requiredWorkingMinutes"] == 510
    assert result.sources["requiredWorkingMinutes"] == "SHOP"
    assert result.values["outOfCameraGraceMinutes"] == 5
    assert result.sources["outOfCameraGraceMinutes"] == "SYSTEM"


def test_explicit_false_and_zero_are_not_discarded():
    result = resolve_attendance_policy(
        {"dayEndAutoLogoutEnabled": False, "allowedBreakMinutes": 0},
        {"allowed_break_minutes": 60},
    )
    assert result.values["dayEndAutoLogoutEnabled"] is False
    assert result.values["allowedBreakMinutes"] == 0
