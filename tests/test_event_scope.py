from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from camera_service.cloud_client import CloudSyncClient
from camera_service.storage import SQLiteStore


def edge():
    return SimpleNamespace(edge_id="edge-1",tenant_id="tenant-1",company_code="2",shop_id="WBTEST",site_id="legacy-site")


def test_event_producer_queue_gets_canonical_scope(tmp_path):
    store=SQLiteStore(str(tmp_path/"edge.db"))
    store.configure_event_scope("tenant-1","2","WBTEST","edge-1")
    event_id=store.add_person_event(None,"WBTEST","cam-7","UNKNOWN_INSIDE_ALERT",datetime.now(timezone.utc),{"track_id":"4"})
    row=store.queued_events()[0]
    import json
    payload=json.loads(row["payload_json"])
    assert payload["scope"] == {
        "tenant_id":"tenant-1","company_code":"2","shop_id":"WBTEST","edge_id":"edge-1","camera_id":"cam-7"
    }


def test_cloud_client_rejects_cross_shop_queue_payload():
    client=CloudSyncClient(SimpleNamespace(enabled=True,base_url="https://example.invalid",api_token="x"))
    event={"event_id":"evt","event_type":"UNKNOWN_PERSON","event_time":"2026-09-26T00:00:00Z","camera_id":"cam-1",
           "scope":{"tenant_id":"tenant-1","company_code":"2","shop_id":"OTHER","edge_id":"edge-1","camera_id":"cam-1"}}
    with pytest.raises(RuntimeError,match="shop_id"):
        client.event_envelope(edge(),event)
