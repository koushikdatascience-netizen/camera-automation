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

class EdgeConfig(BaseModel):
    edge_id: str = "local-edge-01"
    tenant_id: str = "demo-tenant"
    site_id: str = "demo-site"
    activation_required: bool = False
    activation_token: str = ""
    plan: str = "demo"

class CloudSyncConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    api_token: str = ""
    timeout_seconds: float = 10.0
    batch_size: int = 50

class AlertConfig(BaseModel):
    whatsapp_enabled: bool = False
    whatsapp_recipients: list[str] = Field(default_factory=list)
    send_unknown_inside: bool = True
    send_crowd_alerts: bool = True
    send_long_break_alerts: bool = True

class EvidenceConfig(BaseModel):
    snapshot_enabled: bool = True
    clip_enabled: bool = False
    pre_event_seconds: int = 30
    post_event_seconds: int = 30

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
    edge: EdgeConfig = Field(default_factory=EdgeConfig)
    cloud_sync: CloudSyncConfig = Field(default_factory=CloudSyncConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    recognition: RecognitionConfig = Field(default_factory=RecognitionConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)

def load_config(path: str = "config.yaml") -> AppConfig:
    p=Path(path)
    if not p.exists():
        return AppConfig()
    with p.open("r",encoding="utf-8") as f:
        return AppConfig.model_validate(yaml.safe_load(f) or {})
