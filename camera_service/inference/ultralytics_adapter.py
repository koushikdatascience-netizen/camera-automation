from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Callable

from camera_service.domain.identifiers import ResourceScope
from camera_service.domain.inference import BackendType, Detection, InferenceResult


def _to_list(value: Any) -> list:
    if value is None:
        return []
    if hasattr(value, "cpu"):
        value = value.cpu()
    if hasattr(value, "numpy"):
        value = value.numpy()
    if hasattr(value, "tolist"):
        return value.tolist()
    return list(value)


def normalize_ultralytics_results(
    results: Any,
    *,
    scope: ResourceScope,
    frame_id: str,
    model_id: str,
    model_version: str,
    backend_type: BackendType,
    latency_ms: float,
) -> InferenceResult:
    detections: list[Detection] = []
    tracks: list[dict[str, Any]] = []
    result_list = list(results or [])
    if result_list:
        result = result_list[0]
        names = getattr(result, "names", {}) or {}
        boxes = getattr(result, "boxes", None)
        if boxes is not None:
            coords = _to_list(getattr(boxes, "xyxy", None))
            confs = _to_list(getattr(boxes, "conf", None))
            classes = _to_list(getattr(boxes, "cls", None))
            raw_ids = getattr(boxes, "id", None)
            ids = _to_list(raw_ids) if raw_ids is not None else [None] * len(coords)
            for index, xyxy in enumerate(coords):
                class_id = int(classes[index]) if index < len(classes) else -1
                class_name = names.get(class_id, f"class_{class_id}") if isinstance(names, dict) else str(class_id)
                track_id = ids[index] if index < len(ids) else None
                if track_id is not None:
                    track_id = int(track_id)
                detection = Detection(
                    class_name=str(class_name),
                    confidence=float(confs[index]) if index < len(confs) else 0.0,
                    bbox_xyxy=tuple(float(v) for v in xyxy),
                    track_id=track_id,
                    attributes={"class_id": class_id},
                )
                detections.append(detection)
                if track_id is not None:
                    tracks.append(
                        {
                            "track_id": track_id,
                            "class_name": detection.class_name,
                            "confidence": detection.confidence,
                            "bbox_xyxy": detection.bbox_xyxy,
                        }
                    )
    return InferenceResult(
        scope=scope,
        frame_id=frame_id,
        timestamp=datetime.now(timezone.utc),
        model_id=model_id,
        model_version=model_version,
        detections=detections,
        tracks=tracks,
        inference_latency_ms=max(0.0, latency_ms),
        backend_type=backend_type,
    )


class UltralyticsCPUBackend:
    """Phase-2 adapter for the existing Ultralytics CPU inference path.

    The model is injected in tests and lazily loaded in production. No existing
    CameraManager path is replaced yet, which keeps Phase 2 regression-safe.
    """

    backend_type = BackendType.LOCAL_CPU

    def __init__(
        self,
        model_path: str | Path,
        *,
        model_id: str = "person-tracking",
        model_version: str = "current",
        model_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.model_path = str(model_path)
        self.model_id = model_id
        self.model_version = model_version
        self._model_factory = model_factory
        self._model: Any = None
        self.last_error: str | None = None

    def _load(self) -> Any:
        if self._model is not None:
            return self._model
        try:
            if self._model_factory is not None:
                self._model = self._model_factory(self.model_path)
            else:
                from ultralytics import YOLO
                self._model = YOLO(self.model_path)
            self.last_error = None
            return self._model
        except Exception as exc:
            self.last_error = str(exc)
            raise

    def infer(
        self,
        frame: Any,
        *,
        scope: ResourceScope,
        frame_id: str,
        model_id: str | None = None,
        model_version: str | None = None,
        conf: float = 0.20,
        imgsz: int = 384,
        max_det: int = 40,
        tracking: bool = False,
    ) -> InferenceResult:
        model = self._load()
        started = perf_counter()
        if tracking:
            results = model.track(
                frame,
                persist=True,
                tracker="bytetrack.yaml",
                conf=conf,
                imgsz=imgsz,
                max_det=max_det,
                verbose=False,
                device="cpu",
            )
        else:
            results = model.predict(
                frame,
                conf=conf,
                imgsz=imgsz,
                max_det=max_det,
                verbose=False,
                device="cpu",
            )
        latency_ms = (perf_counter() - started) * 1000.0
        return normalize_ultralytics_results(
            results,
            scope=scope,
            frame_id=frame_id,
            model_id=model_id or self.model_id,
            model_version=model_version or self.model_version,
            backend_type=self.backend_type,
            latency_ms=latency_ms,
        )

    def health(self) -> dict[str, Any]:
        return {
            "backend": self.backend_type.value,
            "ready": self.last_error is None,
            "model_path": self.model_path,
            "last_error": self.last_error,
        }
