from __future__ import annotations

import base64
import os
from time import perf_counter

import cv2
import numpy as np
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from camera_service.domain import BackendType, ResourceScope
from camera_service.inference.ultralytics_adapter import normalize_ultralytics_results

router = APIRouter()
_models = {}


class CloudInferenceRequest(BaseModel):
    scope: ResourceScope
    frame_id: str
    model_id: str
    model_version: str
    image_jpeg_base64: str


def _authorize(authorization: str | None):
    expected = os.getenv("SNAPKEY_CLOUD_INFERENCE_TOKEN", "").strip()
    if not expected or authorization != f"Bearer {expected}":
        raise HTTPException(401, "Invalid cloud inference token")


@router.post("/inference/v1/predict")
def predict(request: CloudInferenceRequest, authorization: str | None = Header(default=None)):
    _authorize(authorization)
    model_path = os.getenv("SNAPKEY_CLOUD_GPU_MODEL", "").strip()
    if not model_path:
        raise HTTPException(503, "Cloud GPU model is not configured")
    try:
        raw = base64.b64decode(request.image_jpeg_base64, validate=True)
        frame = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
        if frame is None:
            raise ValueError("invalid image")
    except Exception:
        raise HTTPException(400, "Invalid JPEG payload")
    try:
        from ultralytics import YOLO
        model = _models.get(model_path)
        if model is None:
            model = YOLO(model_path)
            _models[model_path] = model
        started = perf_counter()
        results = model.predict(frame, conf=0.20, imgsz=640, max_det=40, verbose=False, device=0)
        return normalize_ultralytics_results(
            results, scope=request.scope, frame_id=request.frame_id,
            model_id=request.model_id, model_version=request.model_version,
            backend_type=BackendType.CLOUD_GPU, latency_ms=(perf_counter()-started)*1000.0
        ).model_dump(mode="json")
    except Exception as exc:
        raise HTTPException(503, f"Cloud inference unavailable: {type(exc).__name__}")
