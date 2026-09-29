from datetime import datetime, timezone
from types import SimpleNamespace

from camera_service.cloud_client import CloudSyncClient
from camera_service.storage import SQLiteStore
from cloud_portal.storage import PortalStore


def edge():
    return SimpleNamespace(
        edge_id="edge-1",
        tenant_id="tenant-1",
        company_code="2",
        shop_id="WBTEST",
        site_id="legacy-site",
    )


def test_cloud_envelope_carries_canonical_madhushala_scope():
    client = CloudSyncClient(SimpleNamespace(enabled=True, base_url="https://example.invalid", api_token="x"))
    envelope = client.event_envelope(edge(), {
        "event_id": "evt-1",
        "event_type": "ATTENDANCE_ENTRY",
        "event_time": "2026-09-26T00:00:00+00:00",
        "store_id": "legacy-store",
        "camera_id": "cam-1",
    })
    assert envelope["tenant_id"] == "tenant-1"
    assert envelope["company_code"] == "2"
    assert envelope["shop_id"] == "WBTEST"
    assert envelope["edge_id"] == "edge-1"
    assert envelope["camera_id"] == "cam-1"


def test_local_queue_does_not_overwrite_existing_event(tmp_path):
    store = SQLiteStore(str(tmp_path / "edge.db"))
    now = datetime.now(timezone.utc)
    event_id = store.add_person_event(None, "WBTEST", "cam-1", "CAMERA_ONLINE", now, {"v": 1})
    with store._conn() as conn:
        before = dict(conn.execute("SELECT * FROM edge_event_queue WHERE id=?", (event_id,)).fetchone())
        store._enqueue_edge_event(conn, event_id, "CAMERA_ONLINE", {"event_id": event_id, "metadata": {"v": 2}})
    with store._conn() as conn:
        after = dict(conn.execute("SELECT * FROM edge_event_queue WHERE id=?", (event_id,)).fetchone())
    assert before["payload_json"] == after["payload_json"]


def test_failed_event_waits_for_retry_window(tmp_path):
    store = SQLiteStore(str(tmp_path / "edge.db"))
    event_id = store.add_person_event(None, "WBTEST", "cam-1", "CAMERA_OFFLINE", datetime.now(timezone.utc), {})
    store.mark_event_failed(event_id, "offline", retry_after_seconds=60)
    assert all(row["id"] != event_id for row in store.queued_events())


def test_cloud_ingest_is_idempotent_and_keeps_original_event(tmp_path):
    store = PortalStore(str(tmp_path / "portal.db"))
    base = {
        "schema_version": "edge.event.v1",
        "tenant_id": "tenant-1",
        "company_code": "2",
        "shop_id": "WBTEST",
        "site_id": "legacy-site",
        "edge_id": "edge-1",
        "event_id": "evt-1",
        "event_type": "SCISSORS_ALERT",
        "event_time": "2026-09-26T00:00:00+00:00",
        "store_id": "WBTEST",
        "camera_id": "cam-1",
        "payload": {"metadata": {"confidence": 0.9}},
    }
    store.ingest_event(base)
    duplicate = dict(base)
    duplicate["payload"] = {"metadata": {"confidence": 0.1}}
    store.ingest_event(duplicate)
    items = store.list_events("tenant-1")
    assert len(items) == 1
    assert items[0]["company_code"] == "2"
    assert items[0]["shop_id"] == "WBTEST"
    assert items[0]["payload"]["payload"]["metadata"]["confidence"] == 0.9
