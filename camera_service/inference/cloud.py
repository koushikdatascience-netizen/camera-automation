from __future__ import annotations

import base64
from typing import Any

import cv2
import requests

from camera_service.domain.inference import BackendType, InferenceResult
from .base import InferenceBackend


class CloudGPUBackend(InferenceBackend):
    """Optional remote inference client. Disabled unless an explicit endpoint/token is configured."""

    backend_type = BackendType.CLOUD_GPU

    def __init__(self, endpoint: str, token: str, timeout_seconds: float = 10.0):
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds

    def infer(self, frame: Any, *, scope, frame_id: str, model_id: str, model_version: str) -> InferenceResult:
        if not self.endpoint or not self.token:
            raise RuntimeError("Cloud GPU backend is not configured")
        ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
        if not ok:
            raise RuntimeError("Could not encode inference frame")
        payload = {
            "scope": scope.model_dump(),
            "frame_id": frame_id,
            "model_id": model_id,
            "model_version": model_version,
            "image_jpeg_base64": base64.b64encode(encoded.tobytes()).decode("ascii"),
        }
        response = requests.post(
            self.endpoint + "/inference/v1/predict",
            json=payload,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        return InferenceResult.model_validate(response.json())
