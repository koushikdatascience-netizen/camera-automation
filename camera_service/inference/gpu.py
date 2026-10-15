from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from camera_service.domain.inference import BackendType
from camera_service.inference.ultralytics_adapter import UltralyticsCPUBackend


def detect_local_gpu() -> dict[str, Any]:
    """Return deploy-time GPU capability without making GPU a requirement."""
    capability = {"available": False, "provider": None, "device": None, "reason": "no supported GPU runtime detected"}
    try:
        import torch
        if torch.cuda.is_available():
            return {"available": True, "provider": "cuda", "device": "0", "reason": None}
    except Exception:
        pass
    try:
        import onnxruntime as ort
        providers = set(ort.get_available_providers())
        if "DmlExecutionProvider" in providers:
            return {"available": True, "provider": "directml", "device": "dml", "reason": None}
    except Exception:
        pass
    return capability


class UltralyticsGPUBackend(UltralyticsCPUBackend):
    backend_type = BackendType.LOCAL_GPU

    def __init__(self, model_path: str | Path, *, device: str = "0", model_id: str = "person-tracking", model_version: str = "current", model_factory: Callable[[str], Any] | None = None):
        super().__init__(model_path, model_id=model_id, model_version=model_version, model_factory=model_factory)
        self.device = device

    def infer(self, frame: Any, *, scope, frame_id: str, model_id: str | None = None, model_version: str | None = None, conf: float = 0.20, imgsz: int = 384, max_det: int = 40, tracking: bool = False):
        model = self._load()
        from time import perf_counter
        from camera_service.inference.ultralytics_adapter import normalize_ultralytics_results
        started = perf_counter()
        kwargs = dict(conf=conf, imgsz=imgsz, max_det=max_det, verbose=False, device=self.device)
        if tracking:
            results = model.track(frame, persist=True, tracker="bytetrack.yaml", **kwargs)
        else:
            results = model.predict(frame, **kwargs)
        return normalize_ultralytics_results(results, scope=scope, frame_id=frame_id, model_id=model_id or self.model_id, model_version=model_version or self.model_version, backend_type=self.backend_type, latency_ms=(perf_counter()-started)*1000.0)
