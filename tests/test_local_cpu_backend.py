from types import SimpleNamespace

from camera_service.domain import BackendType, ResourceScope
from camera_service.inference.ultralytics_adapter import (
    UltralyticsCPUBackend,
    normalize_ultralytics_results,
)


class TensorLike:
    def __init__(self, value):
        self.value = value

    def cpu(self):
        return self

    def numpy(self):
        return self

    def tolist(self):
        return self.value


class FakeModel:
    def __init__(self):
        self.predict_kwargs = None
        self.track_kwargs = None

    def _result(self, tracked=False):
        boxes = SimpleNamespace(
            xyxy=TensorLike([[10, 20, 110, 220], [2, 4, 20, 40]]),
            conf=TensorLike([0.93, 0.81]),
            cls=TensorLike([0, 1]),
            id=TensorLike([7, 9]) if tracked else None,
        )
        return [SimpleNamespace(names={0: "person", 1: "scissors"}, boxes=boxes)]

    def predict(self, frame, **kwargs):
        self.predict_kwargs = kwargs
        return self._result(False)

    def track(self, frame, **kwargs):
        self.track_kwargs = kwargs
        return self._result(True)


def scope():
    return ResourceScope(
        tenant_id="tenant-1",
        company_code="2",
        shop_id="WBTEST",
        edge_id="edge-1",
        camera_id="camera-1",
    )


def test_cpu_backend_preserves_existing_predict_defaults_and_normalizes():
    model = FakeModel()
    backend = UltralyticsCPUBackend("yolo.pt", model_factory=lambda _: model)
    result = backend.infer(object(), scope=scope(), frame_id="frame-1")

    assert model.predict_kwargs == {
        "conf": 0.20,
        "imgsz": 384,
        "max_det": 40,
        "verbose": False,
        "device": "cpu",
    }
    assert result.backend_type is BackendType.LOCAL_CPU
    assert [d.class_name for d in result.detections] == ["person", "scissors"]
    assert result.detections[0].bbox_xyxy == (10.0, 20.0, 110.0, 220.0)
    assert result.tracks == []


def test_cpu_backend_tracking_uses_bytetrack_and_keeps_track_ids():
    model = FakeModel()
    backend = UltralyticsCPUBackend("yolo.pt", model_factory=lambda _: model)
    result = backend.infer(
        object(), scope=scope(), frame_id="frame-2", tracking=True, imgsz=640
    )

    assert model.track_kwargs["persist"] is True
    assert model.track_kwargs["tracker"] == "bytetrack.yaml"
    assert model.track_kwargs["device"] == "cpu"
    assert model.track_kwargs["imgsz"] == 640
    assert [d.track_id for d in result.detections] == [7, 9]
    assert [track["track_id"] for track in result.tracks] == [7, 9]


def test_normalizer_handles_empty_ultralytics_results():
    result = normalize_ultralytics_results(
        [],
        scope=scope(),
        frame_id="empty",
        model_id="person-tracking",
        model_version="current",
        backend_type=BackendType.LOCAL_CPU,
        latency_ms=0,
    )
    assert result.detections == []
    assert result.tracks == []
