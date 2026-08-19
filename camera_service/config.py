from __future__ import annotations
from pathlib import Path
from typing import Literal
import yaml
from pydantic import BaseModel, Field

class FeatureConfig(BaseModel):
    face_recognition: bool = False
    attendance: bool = False
    unknown_detection: bool = False
    shoplifting: bool = True

class AttendanceLine(BaseModel):
    x1: float; y1: float; x2: float; y2: float
    inside_side: Literal["positive","negative"] = "positive"
    min_crossing_displacement_px: float = 20.0

class RecognitionConfig(BaseModel):
    enabled: bool = True
    known_threshold: float = 0.65
    required_known_observations: int = 2
    minimum_face_quality: float = 0.35
    unknown_confirmation_seconds: float = 3.0
    max_recognition_attempts: int = 5
    known_recheck_seconds: float = 15.0

class CameraConfig(BaseModel):
    camera_id: str
    name: str
    source_type: Literal["rtsp","file","webcam"] = "file"
    source: str
    enabled: bool = True
    fps: float = 6.0
    camera_role: Literal["ENTRANCE_EXIT","GENERAL","SECURITY","SHOPLIFTING"] = "GENERAL"
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    attendance_line: AttendanceLine | None = None

class AppConfig(BaseModel):
    store_id: str = "store-1"
    database_path: str = "data/camera_automation.db"
    evidence_dir: str = "data/evidence"
    yolo_model: str = "yolo11n.pt"
    recognition: RecognitionConfig = Field(default_factory=RecognitionConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)

def load_config(path: str = "config.yaml") -> AppConfig:
    p=Path(path)
    if not p.exists():
        return AppConfig()
    with p.open("r",encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f) or {})
