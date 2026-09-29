import os
from types import SimpleNamespace

import numpy as np

from camera_service.camera_manager import CameraConfig, CameraManager


class FakeBackend:
    def __init__(self):
        self.backend_type = SimpleNamespace(value="LOCAL_CPU")
        self.calls = []

    def infer(self, frame, **kwargs):
        self.calls.append(kwargs)
        detection = SimpleNamespace(
            class_name="person",
            confidence=0.91,
            bbox_xyxy=(10.0, 20.0, 100.0, 200.0),
            track_id=7,
            attributes={"class_id": 0},
        )
        return SimpleNamespace(
            detections=[detection],
            backend_type=self.backend_type,
            inference_latency_ms=12.5,
        )


def test_camera_manager_can_use_normalized_runtime_backend(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPKEY_INFERENCE_ROUTER_ENABLED", "1")
    manager = CameraManager(str(tmp_path / "camera.db"))
    backend = FakeBackend()
    manager._inference_backends["fake.pt"] = backend
    config = CameraConfig(
        camera_id="cam-1",
        name="Test",
        rtsp_url="file.mp4",
        tracking_mode="track",
    )
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    state = {}

    output = manager._annotate_tracking_frame(
        frame,
        "fake.pt",
        camera_config=config,
        stream_state=state,
    )

    assert output.shape == frame.shape
    assert backend.calls
    assert backend.calls[0]["tracking"] is True
    assert backend.calls[0]["scope"].camera_id == "cam-1"
    assert state["inference_backend"] == "LOCAL_CPU"
    assert state["inference_latency_ms"] == 12.5
    assert state["latest_summary"]["people"] == 1


def test_camera_manager_router_can_be_disabled_for_rollback(tmp_path, monkeypatch):
    monkeypatch.setenv("SNAPKEY_INFERENCE_ROUTER_ENABLED", "0")
    manager = CameraManager(str(tmp_path / "camera.db"))
    assert manager._inference_backends == {}
