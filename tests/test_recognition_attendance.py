from datetime import datetime, timezone
from unittest.mock import Mock

from camera_service.attendance_engine import AttendanceEngine
from camera_service.models import IdentitySeen


def identity(person_id="employee-1", track_id="track-1"):
    return IdentitySeen(
        store_id="store-1", camera_id="camera-1", track_id=track_id,
        person_id=person_id, timestamp=datetime.now(timezone.utc),
        confidence=0.95, bbox=(0.0, 0.0, 100.0, 100.0), snapshot_path=None,
    )


def make_engine():
    store = Mock()
    store.create_arrival.return_value = ({"id": "session-1"}, True)
    return AttendanceEngine(store, "store-1"), store


def test_auto_recognition_confirms_arrival_without_line(monkeypatch):
    monkeypatch.setenv("CAMERA_EYE_ATTENDANCE_MODE", "AUTO")
    engine, store = make_engine()
    engine.on_identity(identity())
    assert store.create_arrival.call_args.kwargs["confirmed"] is True
    assert engine.presence["employee-1"].status == "PRESENT"


def test_manual_recognition_does_not_confirm_arrival(monkeypatch):
    monkeypatch.setenv("CAMERA_EYE_ATTENDANCE_MODE", "MANUAL")
    engine, store = make_engine()
    engine.on_identity(identity())
    assert store.create_arrival.call_args.kwargs["confirmed"] is False


def test_recognition_does_not_end_break(monkeypatch):
    monkeypatch.setenv("CAMERA_EYE_ATTENDANCE_MODE", "AUTO")
    engine, store = make_engine()
    person = identity()
    engine.on_identity(person)
    engine.presence[person.person_id].status = "BREAK"
    engine.on_identity(identity(track_id="track-2"))
    assert engine.presence[person.person_id].status == "BREAK"
    assert store.create_arrival.call_args.kwargs["confirmed"] is False


def test_repeated_recognition_never_closes_session(monkeypatch):
    monkeypatch.setenv("CAMERA_EYE_ATTENDANCE_MODE", "AUTO")
    engine, store = make_engine()
    engine.on_identity(identity())
    engine.on_identity(identity(track_id="track-2"))
    assert store.create_arrival.call_count == 2
    store.close_exit.assert_not_called()
