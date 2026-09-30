import threading
import time

import numpy as np

from camera_service.livekit_publisher import LiveKitCameraPublisher


def test_camera_manager_live_ai_frame_is_copy_and_expires(tmp_path):
    from camera_service.camera_manager import CameraManager

    manager = CameraManager(str(tmp_path / "camera.db"))
    frame = np.zeros((24, 32, 3), dtype=np.uint8)
    frame[2, 3] = [10, 20, 30]
    manager.publish_live_ai_frame("attendance-1", frame)

    received = manager.get_live_ai_frame("attendance-1")
    assert received is not None
    assert received.tolist() == frame.tolist()
    received[2, 3] = [99, 99, 99]
    assert manager.get_live_ai_frame("attendance-1")[2, 3].tolist() == [10, 20, 30]

    with manager._live_frame_lock:
        manager._live_frames["attendance-1"]["updated_at"] = time.monotonic() - 5
    assert manager.get_live_ai_frame("attendance-1", max_age_seconds=3) is None


def test_live_view_rejects_non_attendance_camera():
    class Role:
        value = "GENERAL"

    class Camera:
        camera_role = Role()

    class Manager:
        def get_camera(self, camera_id):
            return Camera()

    publisher = LiveKitCameraPublisher(Manager())
    try:
        publisher.start(session_id="s1", camera_id="cam-1", url="wss://example.test", token="token")
    except RuntimeError as exc:
        assert "attendance cameras" in str(exc)
    else:
        raise AssertionError("GENERAL camera should not start a remote live session")


def test_live_view_start_does_not_open_camera_source(monkeypatch):
    class Role:
        value = "ENTRANCE_EXIT"

    class Camera:
        camera_role = Role()

    class Manager:
        def get_camera(self, camera_id):
            return Camera()

    publisher = LiveKitCameraPublisher(Manager())
    called = {}

    def fake_thread_main(session_id, camera_id, url, token, ttl, stop_event):
        called["camera_id"] = camera_id

    monkeypatch.setattr(publisher, "_thread_main", fake_thread_main)
    result = publisher.start(session_id="s1", camera_id="attendance-1", url="wss://example.test", token="token", ttl_seconds=60)
    result_thread = publisher._sessions["s1"].thread
    result_thread.join(timeout=1)
    assert result["started"] is True
    assert called["camera_id"] == "attendance-1"
