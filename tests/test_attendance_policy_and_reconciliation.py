from datetime import datetime, timedelta, timezone

from camera_service.storage import SQLiteStore
from cloud_portal.attendance_policy import AttendancePolicy


def test_authoritative_roster_quarantines_pre_provenance_stale_identity(tmp_path):
    store=SQLiteStore(str(tmp_path / "edge.db"))
    now=store.now()
    stale_id="7b57571c-stale-pintu"
    with store._conn() as conn:
        conn.execute("""INSERT INTO personnel(id,employee_code,full_name,role,phone,email,active,created_at,updated_at,managed_source)
            VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (stale_id,"02","PINTU SINGH","WORKER",None,None,1,now,now,"legacy"))
    store.add_face(stale_id,[0.1,0.2,0.3],0.9)

    current_id="3bee517a-current-pintu"
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
    assert store.get_person(stale_id)["active"] == 0
    # Legacy biometric data is retained for migration/audit safety, but inactive
    # personnel are excluded from the recognition embedding set.
    assert len(store.list_faces(stale_id)) == 1
    assert all(row["person_id"] != stale_id for row in store.embeddings())
    assert store.get_person(current_id)["managed_source"] == "crm"


def test_authoritative_roster_preserves_intentional_local_only_identity(tmp_path):
    store=SQLiteStore(str(tmp_path / "edge.db"))

    class Person:
        employee_code="LOCAL-01"
        full_name="Local Operator"
        role=type("Role", (), {"value":"WORKER"})()
        phone=None
        email=None

    local=store.create_person(Person())
    store.add_face(local["id"],[0.4,0.5,0.6],0.95)
    store.apply_cloud_personnel([])

    assert store.get_person(local["id"])["active"] == 1
    assert store.get_person(local["id"])["managed_source"] == "local"
    assert len(store.list_faces(local["id"])) == 1
    assert any(row["person_id"] == local["id"] for row in store.embeddings())


def test_removed_confirmed_crm_identity_loses_face_template(tmp_path):
    store=SQLiteStore(str(tmp_path / "edge.db"))
    person_id="crm-old-user"
    store.apply_cloud_personnel([{
        "person_id":person_id,
        "employee_code":"CRM-OLD",
        "full_name":"Old CRM User",
        "role":"WORKER",
        "active":True,
        "faces":[{"face_id":"crm-face","embedding":[0.1,0.2,0.3],"quality":0.9}],
    }])
    result=store.apply_cloud_personnel([])

    assert result["deactivated"] == 1
    assert result["removed_faces"] == 1
    assert store.get_person(person_id)["active"] == 0
    assert store.list_faces(person_id) == []
    assert all(row["person_id"] != person_id for row in store.embeddings())

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
