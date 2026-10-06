from datetime import datetime, timedelta, timezone

from camera_service.storage import SQLiteStore
from cloud_portal.attendance_policy import AttendancePolicy


def test_authoritative_roster_deactivates_stale_identity_and_removes_face(tmp_path):
    store=SQLiteStore(str(tmp_path / "edge.db"))

    class Person:
        employee_code="02"
        full_name="Old Pintu"
        role=type("Role", (), {"value":"WORKER"})()
        phone=None
        email=None

    stale=store.create_person(Person())
    store.add_face(stale["id"],[0.1,0.2,0.3],0.9)
    current_id="crm-pintu-uuid"
    result=store.apply_cloud_personnel([{
        "person_id":current_id,
        "employee_code":"PINTU KUMER SINGH",
        "full_name":"PINTU KUMER SINGH",
        "role":"WORKER",
        "active":True,
        "faces":[],
    }])

    assert result["authoritative"] is True
    assert result["deactivated"] == 1
    assert store.get_person(stale["id"])["active"] == 0
    assert store.list_faces(stale["id"]) == []
    assert store.get_person(current_id)["active"] == 1
    assert all(row["person_id"] != stale["id"] for row in store.embeddings())


def test_attendance_policy_absence_requires_healthy_coverage():
    policy=AttendancePolicy(grace_period_minutes=15)
    seen=datetime(2026,10,6,9,0,tzinfo=timezone.utc)
    now=seen+timedelta(minutes=16)
    assert policy.should_auto_logout_for_absence(
        now=now,last_seen_at=seen,checked_in=True,on_break=False,camera_coverage_healthy=True)
    assert not policy.should_auto_logout_for_absence(
        now=now,last_seen_at=seen,checked_in=True,on_break=False,camera_coverage_healthy=False)


def test_attendance_policy_does_not_logout_during_break():
    policy=AttendancePolicy(grace_period_minutes=1)
    seen=datetime(2026,10,6,9,0,tzinfo=timezone.utc)
    assert not policy.should_auto_logout_for_absence(
        now=seen+timedelta(hours=1),last_seen_at=seen,checked_in=True,on_break=True,
        camera_coverage_healthy=True)


def test_max_logoff_deadline_uses_policy_timezone():
    policy=AttendancePolicy(max_logoff_time="21:30",timezone="Asia/Kolkata")
    reference=datetime(2026,10,6,12,0,tzinfo=timezone.utc)
    deadline=policy.max_logoff_deadline(reference)
    assert deadline.hour == 21
    assert deadline.minute == 30
    assert deadline.utcoffset() == timedelta(hours=5,minutes=30)
