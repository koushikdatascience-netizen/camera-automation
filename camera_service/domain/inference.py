from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from .identifiers import ResourceScope


class BackendType(str, Enum):
    LOCAL_CPU = "LOCAL_CPU"
    LOCAL_GPU = "LOCAL_GPU"
    CLOUD_GPU = "CLOUD_GPU"


class Detection(BaseModel):
    class_name: str
    confidence: float = Field(ge=0.0, le=1.0)
    bbox_xyxy: tuple[float, float, float, float]
    track_id: str | int | None = None
    attributes: dict[str, Any] = Field(default_factory=dict)


class InferenceResult(BaseModel):
    schema_version: int = 1
    scope: ResourceScope
    frame_id: str
    timestamp: datetime
    model_id: str
    model_version: str
    detections: list[Detection] = Field(default_factory=list)
    tracks: list[dict[str, Any]] = Field(default_factory=list)
    faces: list[dict[str, Any]] = Field(default_factory=list)
    inference_latency_ms: float = Field(ge=0.0)
    backend_type: BackendType
