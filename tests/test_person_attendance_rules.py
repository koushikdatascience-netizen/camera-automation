from datetime import datetime, timedelta, timezone

import pytest

from cloud_portal.person_attendance_rules import PersonAttendancePolicy, evaluate_absence


NOW = datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)
P = PersonAttendancePolicy()


def evaluate(minutes, **overrides):
    args = dict(now=NOW, last_seen_at=NOW-timedelta(minutes=minutes),
                checked_in=True, on_break=False, camera_coverage_healthy=True,
                completed_episodes_today=0)
    args.update(overrides)
    return evaluate_absence(P, **args)


@pytest.mark.parametrize("minutes,state,expected", [
    (4.9, "PRESENT_OR_GRACE", ()),
    (5, "OUT_OF_CAMERA", ("GRACE_EXCEEDED",)),
    (14.9, "OUT_OF_CAMERA", ("GRACE_EXCEEDED",)),
    (15, "ADMIN_WARNING", ("GRACE_EXCEEDED", "ADMIN_ABSENCE_WARNING")),
    (60, "PROLONGED_ABSENCE", ("GRACE_EXCEEDED", "ADMIN_ABSENCE_WARNING",
                              "PROLONGED_ABSENCE", "CRM_ABSENT_ACTION_PENDING")),
])
def test_thresholds(minutes, state, expected):
    result = evaluate(minutes)
    assert result.state == state
    assert result.transitions == expected


def test_sixth_episode_exceeds_limit():
    assert "DAILY_ABSENCE_LIMIT_EXCEEDED" not in evaluate(
        6, completed_episodes_today=4).transitions
    assert "DAILY_ABSENCE_LIMIT_EXCEEDED" in evaluate(
        6, completed_episodes_today=5).transitions


def test_active_episode_does_not_count_twice():
    result = evaluate(16, completed_episodes_today=5, active_episode_counted=True)
    assert "DAILY_ABSENCE_LIMIT_EXCEEDED" not in result.transitions


def test_camera_failure_not_absence():
    assert evaluate(120, camera_coverage_healthy=False).state == "UNKNOWN_CAMERA_UNHEALTHY"
    assert evaluate(120, camera_coverage_healthy=False).transitions == ()


def test_break_not_absence():
    assert evaluate(120, on_break=True).transitions == ()


def test_manual_mode_does_not_change_monitoring():
    policy = PersonAttendancePolicy(attendance_mode="MANUAL")
    result = evaluate_absence(policy, now=NOW, last_seen_at=NOW-timedelta(minutes=15),
                              checked_in=True, on_break=False, camera_coverage_healthy=True,
                              completed_episodes_today=0)
    assert result.state == "ADMIN_WARNING"


def test_invalid_threshold_order():
    with pytest.raises(ValueError):
        PersonAttendancePolicy(out_of_camera_grace_minutes=15, admin_notification_after_minutes=5)


def test_timezone_required():
    with pytest.raises(ValueError):
        evaluate_absence(P, now=NOW.replace(tzinfo=None), last_seen_at=NOW,
                         checked_in=True, on_break=False, camera_coverage_healthy=True,
                         completed_episodes_today=0)


def test_reappearance_resets_absence_elapsed_time():
    result = evaluate_absence(P, now=NOW, last_seen_at=NOW-timedelta(seconds=30),
                              checked_in=True, on_break=False, camera_coverage_healthy=True,
                              completed_episodes_today=2)
    assert result.state == "PRESENT_OR_GRACE"
    assert result.transitions == ()


def test_business_date_uses_employee_timezone_at_utc_midnight_boundary():
    local_next_day = datetime(2026, 10, 7, 19, 0, tzinfo=timezone.utc)
    result = evaluate_absence(P, now=local_next_day, last_seen_at=local_next_day-timedelta(minutes=20),
                              checked_in=True, on_break=False, camera_coverage_healthy=True,
                              completed_episodes_today=0)
    assert result.business_date == "2026-10-08"
def test_shop_day_boundary_before_and_after_start():
    from datetime import datetime,timezone
    from cloud_portal.person_attendance_rules import business_date
    assert business_date(datetime(2026,10,11,0,29,tzinfo=timezone.utc),'Asia/Kolkata','06:00')=='2026-10-10'
    assert business_date(datetime(2026,10,11,0,30,tzinfo=timezone.utc),'Asia/Kolkata','06:00')=='2026-10-11'
