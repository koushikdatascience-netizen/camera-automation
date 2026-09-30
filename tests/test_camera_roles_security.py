import time

from camera_service.camera_manager import CameraManager


def _camera(manager, camera_id="sec-1", role="SECURITY", attendance_active=True):
    return manager.create_camera({
        "camera_id": camera_id,
        "name": camera_id,
        "source_type": "webcam",
        "rtsp_url": "0" if camera_id == "sec-1" else "1",
        "enabled": True,
        "camera_role": role,
        "attendance_active": attendance_active,
        "features": {
            "attendance": role == "ENTRANCE_EXIT",
            "face_recognition": True,
            "unknown_detection": True,
            "unknown_person_detection": True,
        },
    })


def test_attendance_camera_disables_unknown_security_and_persists_active(tmp_path):
    manager = CameraManager(str(tmp_path / "camera.db"))
    camera = _camera(manager, camera_id="attendance-1", role="ENTRANCE_EXIT", attendance_active=False)
    assert camera.attendance_active is False
    assert camera.features.attendance is True
    assert camera.features.face_recognition is True
    assert camera.features.unknown_enabled is False
    loaded = manager.get_camera("attendance-1")
    assert loaded.attendance_active is False
    assert loaded.features.unknown_enabled is False


def test_security_zone_is_normalized_and_persists(tmp_path):
    manager = CameraManager(str(tmp_path / "camera.db"))
    _camera(manager)
    zone = manager.save_security_zone("sec-1", {
        "name": "Shelf",
        "x": 0.1, "y": 0.2, "width": 0.4, "height": 0.5, "enabled": True,
    })
    assert zone["name"] == "Shelf"
    assert manager._matching_security_zone("sec-1", (20, 30, 40, 50), (100, 100, 3))["id"] == zone["id"]
    assert manager._matching_security_zone("sec-1", (80, 80, 90, 90), (100, 100, 3)) is None


def test_security_unknown_requires_temporal_confirmation(tmp_path, monkeypatch):
    manager = CameraManager(str(tmp_path / "camera.db"))
    _camera(manager)
    manager.save_security_zone("sec-1", {
        "name": "Shelf", "x": 0.0, "y": 0.0, "width": 0.6, "height": 0.6, "enabled": True,
    })
    clock = {"value": 100.0}
    monkeypatch.setattr("camera_service.camera_manager.time.monotonic", lambda: clock["value"])
    assert manager._confirmed_unknown_zone("sec-1", "7", (10, 10, 30, 30), (100, 100, 3), seconds=2.0) is None
    clock["value"] += 1.0
    assert manager._confirmed_unknown_zone("sec-1", "7", (10, 10, 30, 30), (100, 100, 3), seconds=2.0) is None
    clock["value"] += 1.1
    confirmed = manager._confirmed_unknown_zone("sec-1", "7", (10, 10, 30, 30), (100, 100, 3), seconds=2.0)
    assert confirmed and confirmed["name"] == "Shelf"


def test_security_zone_rejects_out_of_bounds(tmp_path):
    manager = CameraManager(str(tmp_path / "camera.db"))
    _camera(manager)
    try:
        manager.save_security_zone("sec-1", {
            "name": "Bad", "x": 0.8, "y": 0.8, "width": 0.4, "height": 0.4,
        })
    except ValueError as exc:
        assert "fit inside" in str(exc)
    else:
        raise AssertionError("Out-of-bounds zone should be rejected")


def test_role_normalization_removes_attendance_security_mix(tmp_path):
    manager = CameraManager(str(tmp_path / "camera.db"))
    attendance = manager.create_camera({
        "camera_id":"att","name":"Attendance","source_type":"webcam","rtsp_url":"0",
        "camera_role":"ENTRANCE_EXIT",
        "features":{"attendance":False,"face_recognition":False,"unknown_detection":True,
                    "unknown_person_detection":True,"shoplifting":True,"object_security":True},
    })
    assert attendance.features.attendance is True
    assert attendance.features.face_recognition is True
    assert attendance.features.unknown_enabled is False
