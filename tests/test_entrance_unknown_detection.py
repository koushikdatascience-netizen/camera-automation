"""Runtime regression coverage for unknown incidents on entrance/security cameras."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from camera_service.camera_manager import CameraConfig, CameraFeatures, CameraManager, CameraRole
from camera_service.config import RecognitionConfig
from camera_service.domain.inference import BackendType
from camera_service.storage import SQLiteStore


def test_entrance_exit_preserves_enabled_unknown_detection():
    camera = CameraConfig(
        camera_id="entrance-test", name="Entrance", rtsp_url="0",
        source_type="webcam", camera_role=CameraRole.ENTRANCE_EXIT,
        features=CameraFeatures(unknown_detection=True, unknown_person_detection=True),
    )
    assert camera.features.attendance is True
    assert camera.features.face_recognition is True
    assert camera.tracking_mode == "track"
    assert camera.features.unknown_detection is True
    assert camera.features.unknown_person_detection is True


def test_entrance_exit_preserves_disabled_unknown_detection():
    camera = CameraConfig(
        camera_id="entrance-test-disabled", name="Entrance", rtsp_url="0",
        camera_role=CameraRole.ENTRANCE_EXIT,
        features=CameraFeatures(unknown_detection=False, unknown_person_detection=False),
    )
    assert camera.features.unknown_detection is False
    assert camera.features.unknown_person_detection is False
    assert camera.features.attendance is True
    assert camera.features.face_recognition is True


class _Backend:
    backend_type = BackendType.LOCAL_CPU

    def __init__(self, camera_id, known=False):
        self.camera_id = camera_id
        self.known = known

    def infer(self, frame, **_kwargs):
        detection = SimpleNamespace(
            class_name="person", confidence=0.95, bbox_xyxy=(10.0, 10.0, 90.0, 90.0),
            track_id="stable-track", attributes={"class_id": 0},
        )
        return SimpleNamespace(detections=[detection], backend_type=self.backend_type,
                               inference_latency_ms=1.0)


class _FaceService:
    def __init__(self, kind="unknown"):
        self.kind = kind
        self.detect_calls = 0
        self.detect_times = []

    def detect(self, _roi):
        self.detect_calls += 1
        from camera_service.camera_manager import time as camera_time
        self.detect_times.append(camera_time.monotonic())
        if self.kind == "failure":
            raise RuntimeError("recognition unavailable")
        if self.kind == "no_embedding":
            return [{"embedding": None}]
        return [{"embedding": [0.1, 0.2], "bbox": (5, 5, 35, 35)}]

    def quality(self, _face, _shape):
        return 0.1 if self.kind == "blurry" else 0.95

    def recognize(self, _embedding, _threshold):
        if self.kind == "known":
            return ({"person_id": "person-1", "full_name": "Known Person"}, 0.93)
        return None, 0.12


class _AttendanceEngine:
    store_id = "tenant-shop-1395"

    def __init__(self):
        self.identities = []

    def on_identity(self, identity, recognition_event_id=None):
        self.identities.append(identity)


def _pipeline(tmp_path, monkeypatch, role="ENTRANCE_EXIT", enabled=True, face_kind="unknown",
              face_recheck=0.75, confirmation_seconds=2.0, diagnostics=False):
    monkeypatch.setenv("SNAPKEY_INFERENCE_ROUTER_ENABLED", "1")
    manager = CameraManager(str(tmp_path / "camera-manager.db"))
    camera_id = f"stable-camera-{role.lower()}"
    camera = manager.create_camera({
        "camera_id": camera_id, "name": camera_id, "source_type": "webcam", "rtsp_url": "0",
        "camera_role": role, "camera_zone": "inside", "attendance_active": True,
        "features": {"attendance": role == "ENTRANCE_EXIT", "face_recognition": True,
                     "unknown_detection": enabled, "unknown_person_detection": enabled},
    })
    store = SQLiteStore(str(tmp_path / "edge.db"))
    store.configure_event_scope("tenant-1395", "company-1", "shop-1395", "edge-1")
    manager.save_security_zone(camera_id, {
        "name": "Entrance confirmation", "x": 0.0, "y": 0.0, "width": 1.0, "height": 1.0,
    })
    manager._inference_backends[("fake.pt", camera_id)] = _Backend(camera_id)
    monkeypatch.setattr(manager, "_save_event_snapshot", lambda _frame, cam, prefix: f"evidence/{cam}/{prefix}.jpg")
    monkeypatch.setattr(manager, "_begin_unknown_clip", lambda state, incident_id, cam: state.update({
        "security_clip": {"alert_id": incident_id, "camera_id": cam, "event_kind": "unknown"}
    }))
    clock = {"value": 100.0}
    monkeypatch.setattr("camera_service.camera_manager.time.monotonic", lambda: clock["value"])
    engine = _AttendanceEngine()
    config = RecognitionConfig(enabled=True, minimum_face_quality=0.25,
        known_recheck_seconds=face_recheck, unknown_confirmation_seconds=confirmation_seconds,
        unknown_detection_diagnostics=diagnostics)
    face_service = _FaceService(face_kind)
    state = {}
    def observe(kind=face_kind, advance=1.0):
        clock["value"] += advance
        manager._annotate_tracking_frame(np.zeros((100, 100, 3), dtype=np.uint8), "fake.pt",
            face_service if kind == face_kind else _FaceService(kind), config, camera, engine, store, state)
    observe.face_service = face_service
    return manager, camera, store, engine, state, observe


@pytest.mark.parametrize("role", ["ENTRANCE_EXIT", "SECURITY"])
def test_unknown_face_creates_incident_evidence_and_scoped_sync_event(tmp_path, monkeypatch, role):
    manager, camera, store, _engine, state, observe = _pipeline(tmp_path, monkeypatch, role=role)
    observe(advance=0.0)
    assert store.unknowns() == []  # one observation cannot confirm a zone
    observe(advance=1.0)
    assert store.unknowns() == []
    observe(advance=1.1)
    incidents = store.unknowns()
    assert len(incidents) == 1
    incident = incidents[0]
    assert incident["camera_id"] == camera.camera_id
    assert incident["best_face_snapshot"] == f"evidence/{camera.camera_id}/unknown_face.jpg"
    assert incident["best_person_snapshot"] == f"evidence/{camera.camera_id}/unknown_person.jpg"
    assert state["security_clip"]["alert_id"] == incident["id"]
    with store._conn() as conn:
        queued = conn.execute("SELECT event_type,payload_json FROM edge_event_queue WHERE id=?", (incident["id"],)).fetchone()
    payload = json.loads(queued["payload_json"])
    assert queued["event_type"] == "UNKNOWN_INCIDENT"
    assert payload["camera_id"] == camera.camera_id
    assert payload["scope"]["camera_id"] == camera.camera_id
    assert payload["metadata"]["face_path"] == incident["best_face_snapshot"]
    assert payload["metadata"]["person_path"] == incident["best_person_snapshot"]


def test_entrance_unknown_confirms_at_three_second_face_retry_on_three_fps_frames(
    tmp_path, monkeypatch, caplog
):
    manager, camera, store, _engine, _state, observe = _pipeline(
        tmp_path, monkeypatch, role="ENTRANCE_EXIT", face_recheck=3.0,
        confirmation_seconds=3.0, diagnostics=True,
    )
    import logging
    caplog.set_level(logging.INFO, logger="camera_service.camera_manager")
    observe(advance=0.0)
    # Keep the camera's person-track presence alive at 3 FPS while recognition
    # itself is only retried every three seconds.
    for _ in range(10):
        observe(advance=1.0 / 3.0)
    assert observe.face_service.detect_calls >= 2
    assert observe.face_service.detect_times[-1] - observe.face_service.detect_times[0] >= 3.0
    incidents = store.unknowns()
    assert len(incidents) == 1
    assert incidents[0]["camera_id"] == camera.camera_id
    diagnostic_lines = [record.message for record in caplog.records
                        if "unknown_detection_decision" in record.message]
    assert any(camera.camera_id in line and "stable-track" in line
               and "incident_created" in line and "confirmation_duration_seconds" in line
               for line in diagnostic_lines)
    assert all("embedding" not in line and "image" not in line for line in diagnostic_lines)


def test_known_face_does_not_create_unknown_and_keeps_attendance_recognition(tmp_path, monkeypatch):
    _manager, _camera, store, engine, _state, observe = _pipeline(tmp_path, monkeypatch, face_kind="known")
    observe(advance=0.0)
    assert store.unknowns() == []
    assert store.person_events("person-1")
    assert engine.identities


def test_unknown_feature_disabled_creates_no_incident(tmp_path, monkeypatch):
    _manager, _camera, store, _engine, _state, observe = _pipeline(tmp_path, monkeypatch, enabled=False)
    observe(advance=0.0)
    observe(advance=1.0)
    observe(advance=1.1)
    assert store.unknowns() == []


@pytest.mark.parametrize("face_kind", ["failure", "no_embedding", "blurry"])
def test_recognition_failure_or_missing_embedding_never_confirms_unknown(tmp_path, monkeypatch, face_kind):
    _manager, _camera, store, _engine, _state, observe = _pipeline(tmp_path, monkeypatch, face_kind=face_kind)
    observe(advance=0.0)
    observe(advance=1.0)
    observe(advance=1.1)
    assert store.unknowns() == []


def test_temporal_zone_confirmation_resets_after_observation_gap(tmp_path, monkeypatch):
    manager = CameraManager(str(tmp_path / "camera-manager.db"))
    camera_id = "stable-entrance"
    manager.create_camera({"camera_id": camera_id, "name": camera_id, "source_type": "webcam",
        "rtsp_url": "0", "camera_role": "ENTRANCE_EXIT",
        "features": {"unknown_detection": True}})
    manager.save_security_zone(camera_id, {"name": "Entry", "x": 0, "y": 0, "width": 1, "height": 1})
    clock = {"value": 10.0}
    monkeypatch.setattr("camera_service.camera_manager.time.monotonic", lambda: clock["value"])
    bbox, shape = (10, 10, 30, 30), (100, 100, 3)
    assert manager._confirmed_unknown_zone(camera_id, "t1", bbox, shape, seconds=2.0) is None
    clock["value"] += 3.0
    assert manager._confirmed_unknown_zone(camera_id, "t1", bbox, shape, seconds=2.0) is None
    clock["value"] += 1.0
    assert manager._confirmed_unknown_zone(camera_id, "t1", bbox, shape, seconds=2.0) is None
    clock["value"] += 1.1
    assert manager._confirmed_unknown_zone(camera_id, "t1", bbox, shape, seconds=2.0) is not None


def test_duplicate_unknown_observations_create_one_incident_and_preserve_attendance(tmp_path, monkeypatch):
    manager, camera, store, engine, _state, observe = _pipeline(tmp_path, monkeypatch)
    observe(advance=0.0)
    observe(advance=1.0)
    observe(advance=1.1)
    # Same stable track is then seen again; the open incident and cooldown dedupe it.
    observe(advance=1.0)
    assert len(store.unknowns()) == 1
    assert store.unknowns()[0]["camera_id"] == camera.camera_id
    assert not engine.identities  # unmatched identity must not affect attendance
