from datetime import datetime, timedelta, timezone

from cloud_portal.postgres_storage import PostgresPortalStore
from fastapi import HTTPException


class _Result:
    def __init__(self, row=None):
        self.row = row
        self.rowcount = 1

    def first(self):
        return self.row

    def mappings(self):
        return self

    def all(self):
        return getattr(self, "rows", [])


class _Connection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.statements = []

    def execute(self, statement, params=None):
        self.statements.append((str(statement), params or {}))
        return next(self.responses, _Result())


class _ConnectionContext:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, *_args):
        return False


def test_crm_auto_logout_claim_never_reclaims_in_flight_action(monkeypatch):
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    connection = _Connection([_Result(), _Result(row=None)])
    store = object.__new__(PostgresPortalStore)
    store.now = lambda: now
    store._conn = lambda: _ConnectionContext(connection)

    claimed = store.claim_crm_auto_logout("tenant", "shop", "user", now - timedelta(hours=1))

    assert claimed is False
    update_sql, params = connection.statements[2]
    assert "status='PENDING' AND attempts<5" in update_sql
    assert "FROM attendance_presence p" in update_sql
    assert "p.last_seen_at=:started" in update_sql
    assert "claimed_at<" not in update_sql
    assert "stale" not in params


def test_attendance_coverage_uses_only_enabled_cameras_in_the_same_zone(monkeypatch):
    now = datetime(2026, 10, 8, tzinfo=timezone.utc)
    received = now - timedelta(seconds=5)
    status = {"cameras": [
        {"camera_id": "other", "camera_role": "ENTRANCE_EXIT", "camera_zone": "inside", "enabled": True, "online": True},
        {"camera_id": "person-camera", "camera_role": "ENTRANCE_EXIT", "camera_zone": "inside", "enabled": True, "online": False},
        {"camera_id": "outside", "camera_role": "ENTRANCE_EXIT", "camera_zone": "outside", "enabled": True, "online": True},
    ]}
    result = _Result()
    result.rows = [
        {"camera_id":"other","camera_zone":"inside","enabled":True,"edge_id":"edge","received_at":received,"status_json":status},
        {"camera_id":"person-camera","camera_zone":"inside","enabled":True,"edge_id":"edge","received_at":received,"status_json":status},
    ]
    connection = _Connection([result])
    store = object.__new__(PostgresPortalStore)
    store._conn = lambda: _ConnectionContext(connection)

    inside=store.attendance_camera_coverage_status("tenant","shop",now,camera_id="person-camera",camera_zone="inside")
    assert inside["state"]=="HEALTHY"
    assert inside["healthyCameraIds"]==["other"]
    outside=store.attendance_camera_coverage_status("tenant","shop",now,camera_id="person-camera",camera_zone="outside")
    assert outside["state"]=="UNKNOWN"

    stale = _Result()
    stale.rows = [{"camera_id":"other","camera_zone":"inside","enabled":True,"edge_id":"edge",
                   "received_at":now-timedelta(seconds=90),"status_json":status}]
    store._conn = lambda: _ConnectionContext(_Connection([stale]))
    assert store.attendance_camera_coverage_status("tenant","shop",now,camera_id="other",camera_zone="inside")["state"]=="UNKNOWN"


def test_auto_logout_requires_full_sixty_minutes_and_uses_camera_scope(monkeypatch):
    from cloud_portal import api

    now = datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc)
    row = {"tenant_id": "tenant", "shop_id": "shop", "crm_user_id": "user",
           "checked_in": True, "on_break": False,
           "last_seen_at": now - timedelta(minutes=59), "last_camera_id": "cam",
           "last_camera_zone": "inside"}
    monkeypatch.setenv("CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED", "true")
    row["policy_json"] = {"attendanceMode": "MANUAL", "absenceMonitoringEnabled": True,
                           "markAbsentAfterMinutes": 60}
    calls = []
    monkeypatch.setattr(api, "store", type("Store", (), {
        "attendance_camera_coverage_healthy": lambda self, *args, **kwargs: calls.append((args, kwargs)) or True,
        "claim_crm_auto_logout": lambda self, *_args: calls.append("claim") or True,
        "get_crm_face_token": lambda self, *_args: None,
        "complete_crm_auto_logout": lambda self, *_args: None,
        "complete_presence_checkout": lambda self, *_args: None,
        "record_attendance_activity": lambda self, *_args: None,
        "mark_crm_auto_logout_confirmed": lambda self, *_args: calls.append("crm-confirmed") or True,
        "finalize_crm_auto_logout_local": lambda self, *_args: calls.append("local-finalized") or "SUCCEEDED",
        "mark_crm_auto_logout_reconciliation_required": lambda self, *_args: calls.append("reconcile"),
        "release_crm_auto_logout_for_retry": lambda self, *_args: calls.append("retry"),
    })())
    monkeypatch.setattr(api, "_v2_face_token", lambda *_args: "mock-token")
    monkeypatch.setattr(api.crm_client, "auto_logout_with_face_token", lambda *_args: {"success": True})
    monkeypatch.setattr(api, "_crm_mutation_succeeded", lambda _result: True)

    api._v2_auto_logout(row, now)
    assert calls == []

    row["last_seen_at"] = now - timedelta(minutes=61)
    api._v2_auto_logout(row, now)
    assert calls[0][1]["camera_id"] == "cam"
    assert calls[1] == "claim"
    assert calls[2:] == ["crm-confirmed", "local-finalized"]

    call_count = len(calls)
    row["policy_json"]["absenceMonitoringEnabled"] = False
    api._v2_auto_logout(row, now)
    row["policy_json"]["absenceMonitoringEnabled"] = True
    row["on_break"] = True
    api._v2_auto_logout(row, now)
    row["on_break"] = False
    row["checked_in"] = False
    api._v2_auto_logout(row, now)
    assert len(calls) == call_count

    row["checked_in"] = True
    row["last_camera_id"] = None
    api._v2_auto_logout(row, now)
    assert len(calls) == call_count


def test_manual_policy_updates_presence_without_crm_check_in(monkeypatch):
    from types import SimpleNamespace
    from cloud_portal import api

    touched = []
    monkeypatch.setattr(api, "store", SimpleNamespace(
        crm_person_mapping=lambda *_args: {"crm_user_id": "crm-user"},
        touch_attendance_presence=lambda **kwargs: touched.append(kwargs),
        person_attendance_policy=lambda *_args: {"attendanceMode": "MANUAL"},
    ))
    monkeypatch.setattr(api, "crm_client", SimpleNamespace(face_attendance_configured=True))
    monkeypatch.setattr(api, "_portal_camera_lookup",
                        lambda *_args: {"camera_role": "ENTRANCE_EXIT"})

    api._auto_attend_recognized_person({
        "event_id": "recognition-1", "event_type": "PERSON_RECOGNIZED",
        "tenant_id": "tenant", "shop_id": "shop", "edge_id": "edge",
        "camera_id": "cam", "event_time": "2026-10-08T06:00:00Z",
        "payload": {"person_id": "local-person"},
    })

    assert len(touched) == 1
    assert touched[0]["checked_in"] is None


def test_manual_policy_blocks_legacy_automatic_entry_and_exit(monkeypatch):
    from types import SimpleNamespace
    from cloud_portal import api

    calls = []
    monkeypatch.setattr(api, "store", SimpleNamespace(
        crm_person_mapping=lambda *_args: {"crm_user_id": "crm-user"},
        person_attendance_policy=lambda *_args: {"attendanceMode": "MANUAL"},
    ))
    monkeypatch.setattr(api, "crm_client", SimpleNamespace(
        configured=True, login_logout=lambda payload: calls.append(payload)))
    base = {"tenant_id": "tenant", "shop_id": "shop", "site_id": "site",
            "event_time": "2026-10-08T06:00:00Z", "payload": {"person_id": "person"}}

    for event_type in ("ATTENDANCE_ENTRY", "ATTENDANCE_EXIT"):
        api._deliver_crm_attendance_event({**base, "event_type": event_type})

    assert calls == []


def test_production_crm_integration_accepts_dynamic_scopes_only_from_trusted_backend(monkeypatch):
    from cloud_portal import api

    class _Request:
        headers = {"X-CRM-Integration-Key": "secret"}
        method = "GET"
        url = type("_Url", (), {"path": "/integration/v2"})()

    monkeypatch.setenv("SNAPKEY_ENV", "production")
    monkeypatch.setenv("SNAPKEY_CRM_INTEGRATION_KEY", "secret")
    monkeypatch.delenv("SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES", raising=False)
    # CRM dynamically supplies tenant/shop identity; no static env IDs.
    api._require_crm_integration(_Request(), "tenant-a", "shop-1")
    api._require_crm_integration(_Request(), "tenant-b", "shop-2")

    class _UntrustedRequest:
        headers = {"X-CRM-Integration-Key": "invalid"}
        method = "GET"
        url = type("_Url", (), {"path": "/integration/v2"})()

    try:
        api._require_crm_integration(_UntrustedRequest(), "tenant-a", "shop-1")
    except HTTPException as exc:
        assert exc.status_code == 401
    else:
        raise AssertionError("untrusted callers must never select CRM tenant/shop scope")
