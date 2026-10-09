"""Unknown-person incidents must sync even when no clip is recorded."""
import json
import sqlite3
from datetime import datetime, timezone

from camera_service.storage import SQLiteStore


def test_unknown_event_not_blocked_by_missing_clip(tmp_path):
    store = SQLiteStore(tmp_path / "unknown.db")
    now = datetime.now(timezone.utc)
    event_id, created = store.upsert_unknown(
        "store-1", "camera-1", "track-1", now, now, now, 3, 0.1,
        None, "/tmp/person.jpg", None,
    )
    assert created
    event = next(row for row in store.queued_events() if row["id"] == event_id)
    metadata = json.loads(event["payload_json"])["metadata"]
    assert metadata["evidence_pending"] is False
    assert metadata["evidence_status"] == "PARTIAL"


def test_existing_unknown_pending_event_recovered_on_restart(tmp_path):
    path = tmp_path / "unknown.db"
    store = SQLiteStore(path)
    now = datetime.now(timezone.utc)
    event_id, _ = store.upsert_unknown(
        "store-1", "camera-1", "track-1", now, now, now, 3, 0.1,
        None, "/tmp/person.jpg", None,
    )
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT payload_json FROM edge_event_queue WHERE id=?", (event_id,)
        ).fetchone()
        payload = json.loads(row[0])
        payload["metadata"]["evidence_pending"] = True
        connection.execute(
            "UPDATE edge_event_queue SET payload_json=? WHERE id=?",
            (json.dumps(payload), event_id),
        )
    restarted = SQLiteStore(path)
    event = next(row for row in restarted.queued_events() if row["id"] == event_id)
    assert json.loads(event["payload_json"])["metadata"]["evidence_pending"] is False
