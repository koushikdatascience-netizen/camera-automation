import json
import sqlite3

import pytest
from fastapi.testclient import TestClient

from cloud_portal import api
from cloud_portal.storage import PortalStore


KEY = "isolated-crm-service-key"


@pytest.fixture
def portal(tmp_path, monkeypatch):
    db = PortalStore(str(tmp_path / "isolated.db"))
    # Policy storage is PostgreSQL-only; this fixture tests the HTTP auth boundary.
    monkeypatch.setattr(db, "attendance_policy", lambda tenant, shop: {}, raising=False)
    monkeypatch.setattr(api, "store", db)
    monkeypatch.setenv("SNAPKEY_CRM_INTEGRATION_KEY", KEY)
    monkeypatch.delenv("SNAPKEY_CRM_ATTENDANCE_QUARANTINED_EVENT_IDS", raising=False)
    return db, TestClient(api.app)


@pytest.mark.parametrize("tenant,shop,expected", [
    ("tenant-a", "shop-a", 200), ("tenant-b", "shop-a", 403),
    ("tenant-a", "shop-b", 403), ("tenant-a", "SHOP-A", 403),
])
def test_integration_exact_credential_binding(portal, tenant, shop, expected):
    db, client = portal
    db.set_crm_integration_scope(api._token_digest(KEY), "tenant-a", "shop-a", granted_by="operator")
    response = client.get(f"/integration/v1/tenants/{tenant}/shops/{shop}/attendance-policy",
                          headers={"X-CRM-Integration-Key": KEY})
    assert response.status_code == expected
    assert KEY not in open(db.path, "rb").read().decode("latin1")


def test_unbound_revoked_and_rotated_credentials_fail_closed(portal, monkeypatch):
    db, client = portal
    url = "/integration/v1/tenants/tenant-a/shops/shop-a/attendance-policy"
    headers = {"X-CRM-Integration-Key": KEY}
    monkeypatch.setenv("SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES", '[{"tenant_id":"tenant-a","shop_id":"shop-a"}]')
    assert client.get(url, headers=headers).status_code == 403
    db.set_crm_integration_scope(api._token_digest(KEY), "tenant-a", "shop-a", granted_by="operator")
    assert client.get(url, headers=headers).status_code == 200
    db.set_crm_integration_scope(api._token_digest(KEY), "tenant-a", "shop-a", granted_by="operator", enabled=False)
    assert client.get(url, headers=headers).status_code == 403
    monkeypatch.setenv("SNAPKEY_CRM_INTEGRATION_KEY", "rotated-isolated-key")
    assert client.get(url, headers=headers).status_code == 401
    assert client.get(url, headers={"X-CRM-Integration-Key":"rotated-isolated-key"}).status_code == 403


def test_crm_session_body_cannot_escape_scope(portal):
    db, client = portal
    db.set_crm_integration_scope(api._token_digest(KEY), "tenant-a", "shop-a", granted_by="operator")
    headers = {"X-CRM-Integration-Key": KEY}
    for tenant, shop in [("tenant-b","shop-a"), ("tenant-a","shop-b")]:
        assert client.post("/crm/session", headers=headers,
                           json={"tenantId":tenant,"shopCode":shop,"role":"OWNER"}).status_code == 403
    assert client.post("/crm/session", headers=headers,
                       json={"tenantId":"tenant-a","shopCode":"shop-a","role":"OWNER"}).status_code == 200


def test_multiple_explicit_grants_and_no_wildcard(portal):
    db, _ = portal
    digest = api._token_digest(KEY)
    for index in range(50):
        db.set_crm_integration_scope(digest, f"tenant-{index}", f"shop-{index}", granted_by="operator")
    assert all(db.crm_integration_scope_allowed(digest, f"tenant-{i}", f"shop-{i}") for i in range(50))
    assert not db.crm_integration_scope_allowed(digest, "tenant-0", "shop-1")
    assert not db.crm_integration_scope_allowed(digest, "*", "*")
    with pytest.raises(ValueError):
        db.set_crm_integration_scope(digest, "*", "*", granted_by="operator")


@pytest.mark.parametrize("config", ["not-json", "{}", '[1]', '"event"'])
def test_invalid_quarantine_fails_closed(monkeypatch, config):
    monkeypatch.setenv("SNAPKEY_CRM_ATTENDANCE_QUARANTINED_EVENT_IDS", config)
    assert api._attendance_quarantined({"event_id":"new-event"})


def test_nine_historical_receipts_untouched_and_no_crm_on_startup_or_redelivery(portal, monkeypatch):
    db, _ = portal
    ids = [f"history-{i}" for i in range(9)]
    monkeypatch.setenv("SNAPKEY_CRM_ATTENDANCE_QUARANTINED_EVENT_IDS", json.dumps(ids))
    envelopes = []
    for index, event_id in enumerate(ids):
        envelope = {"schema_version":"edge.event.v1", "event_id":event_id,
                    "tenant_id":"tenant-a", "shop_id":"shop-a", "edge_id":"edge-a",
                    "site_id":"shop-a", "camera_id":"camera-a", "event_type":"ATTENDANCE_ENTRY",
                    "event_time":"2026-10-09T12:00:00Z", "payload":{"person_id":"synthetic-person",
                    "metadata":{"attendance_session_id":f"session-{index}", "attendance_source":"MANUAL",
                                "attendance_sync_bridge":True}}}
        db.ingest_event(envelope)
        db.register_attendance_delivery(envelope)
        db.set_attendance_delivery(envelope, "RECONCILIATION_REQUIRED" if index == 8 else "RETRY")
        envelopes.append(envelope)
    def snapshot():
        with sqlite3.connect(db.path) as conn:
            return conn.execute("SELECT * FROM edge_attendance_delivery ORDER BY event_id").fetchall()
    before = snapshot()
    monkeypatch.setattr(api, "_crm_face_token", lambda *args: pytest.fail("CRM authentication must not run"))
    with TestClient(api.app) as client:
        assert client.get("/health").status_code == 200
    for envelope in envelopes:
        assert api._synchronize_attendance_bridge(envelope)["error_code"] == "RELEASE_HISTORICAL_QUARANTINE"
        api._deliver_crm_attendance_event(envelope)
    assert snapshot() == before
    assert not api._attendance_quarantined({"event_id":"new-manual-event"})
