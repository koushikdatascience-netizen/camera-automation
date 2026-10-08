from __future__ import annotations
import os
import sys
from pathlib import Path
from typing import Literal
import yaml
from pydantic import BaseModel, Field

class FeatureConfig(BaseModel):
    face_recognition: bool = False
    attendance: bool = False
    unknown_detection: bool = False
    unknown_person_detection: bool = False
    shoplifting: bool = True
    shoplifting_detection: bool = True
    object_security: bool = False

    @property
    def unknown_enabled(self) -> bool:
        return self.unknown_detection or self.unknown_person_detection

    @property
    def shoplifting_enabled(self) -> bool:
        return self.shoplifting or self.shoplifting_detection


class ObjectSecurityInferenceConfig(BaseModel):
    imgsz: int = 960
    confidence: float = 0.55


class ObjectSecurityRoiConfig(BaseModel):
    enabled: bool = False
    x1: int = 0
    y1: int = 0
    x2: int = 0
    y2: int = 0


class ObjectSecurityTilingConfig(BaseModel):
    enabled: bool = False
    tile_size: int = 640
    overlap: float = 0.20


class ObjectSecurityConfirmationConfig(BaseModel):
    enabled: bool = True
    window_frames: int = 5
    required_hits: int = 2
    minimum_confidence: float = 0.45


class ObjectSecurityAlertConfig(BaseModel):
    beep_enabled: bool = True
    beep_frequency: int = 880
    beep_duration_ms: int = 400
    cooldown_seconds: float = 15.0
    save_snapshot: bool = True


class ObjectSecurityConfig(BaseModel):
    enabled: bool = False
    object_classes: list[str] = Field(default_factory=lambda: ["scissors"])
    model_storage_dir: str = "data/models/object_security"
    inference: ObjectSecurityInferenceConfig = Field(default_factory=ObjectSecurityInferenceConfig)
    roi: ObjectSecurityRoiConfig = Field(default_factory=ObjectSecurityRoiConfig)
    tiling: ObjectSecurityTilingConfig = Field(default_factory=ObjectSecurityTilingConfig)
    confirmation: ObjectSecurityConfirmationConfig = Field(default_factory=ObjectSecurityConfirmationConfig)
    alert: ObjectSecurityAlertConfig = Field(default_factory=ObjectSecurityAlertConfig)

class AttendanceLine(BaseModel):
    x1: float; y1: float; x2: float; y2: float
    inside_side: Literal["positive","negative"] = "positive"
    min_crossing_displacement_px: float = 20.0

class RecognitionConfig(BaseModel):
    enabled: bool = True
    known_threshold: float = 0.55
    required_known_observations: int = 2
    minimum_face_quality: float = 0.25
    unknown_confirmation_seconds: float = 3.0
    max_recognition_attempts: int = 5
    known_recheck_seconds: float = 2.0
    unknown_detection_diagnostics: bool = False

class EdgeConfig(BaseModel):
    edge_id: str = "local-edge-01"
    tenant_id: str = "demo-tenant"
    company_code: str | None = None
    shop_id: str = "demo-shop"
    site_id: str = "demo-site"
    activation_required: bool = False
    activation_token: str = ""
    plan: str = "demo"
    license_cache_path: str = "data/license_cache.json"
    license_public_key: str = ""

class CloudSyncConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    api_token: str = ""
    timeout_seconds: float = 10.0
    batch_size: int = 50
    interval_seconds: float = 15.0

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

class RuntimeProfile(BaseModel):
    model: str = "yolo26n.pt"
    tracking_fps: float = 2.0
    tracking_imgsz: int = 384
    tracking_quality: int = 60
    tracking_mode: Literal["detect","track"] = "detect"
    object_security_imgsz: int = 512
    face_recheck_seconds: float = 3.0

class PersonModelConfig(BaseModel):
    model_id: str = "person-detection"
    family: str = "yolo26"
    version: str = "yolo26n"
    storage_dir: str = "data/models/person_detection"
    preferred_runtime: Literal["AUTO","OPENVINO","ONNX","PYTORCH"] = "AUTO"
    openvino_artifact: str = "yolo26n_openvino_model"
    onnx_artifact: str = "yolo26n.onnx"
    pytorch_artifact: str = "yolo26n.pt"


class RuntimeConfig(BaseModel):
    person_model: PersonModelConfig = Field(default_factory=PersonModelConfig)
    profile: Literal["auto","low_power","balanced","performance","cloud_assist"] = "auto"
    inference_backend: Literal["AUTO","LOCAL_CPU","LOCAL_GPU","CLOUD_GPU"] = "AUTO"
    low_power: RuntimeProfile = Field(default_factory=lambda: RuntimeProfile(
        model="yolo26n.pt", tracking_fps=1.0, tracking_imgsz=320, tracking_quality=45,
        tracking_mode="detect", object_security_imgsz=416, face_recheck_seconds=5.0,
    ))
    balanced: RuntimeProfile = Field(default_factory=lambda: RuntimeProfile(
        model="yolo26n.pt", tracking_fps=3.0, tracking_imgsz=384, tracking_quality=60,
        tracking_mode="detect", object_security_imgsz=512, face_recheck_seconds=3.0,
    ))
    performance: RuntimeProfile = Field(default_factory=lambda: RuntimeProfile(
        model="yolo11m.pt", tracking_fps=6.0, tracking_imgsz=640, tracking_quality=70,
        tracking_mode="track", object_security_imgsz=640, face_recheck_seconds=2.0,
    ))
    cloud_assist: RuntimeProfile = Field(default_factory=lambda: RuntimeProfile(
        model="yolo26n.pt", tracking_fps=2.0, tracking_imgsz=384, tracking_quality=60,
        tracking_mode="detect", object_security_imgsz=512, face_recheck_seconds=4.0,
    ))

class CameraConfig(BaseModel):
    camera_id: str
    name: str
    source_type: Literal["rtsp","file","webcam"] = "file"
    source: str
    enabled: bool = True
    rotation_degrees: Literal[0, 90, 180, 270] = 0
    fps: float = 6.0
    camera_role: Literal["ENTRANCE_EXIT","GENERAL","SECURITY","SHOPLIFTING"] = "GENERAL"
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    attendance_line: AttendanceLine | None = None

class AppConfig(BaseModel):
    store_id: str = "store-1"
    database_path: str = "data/camera_automation.db"
    evidence_dir: str = "data/evidence"
    yolo_model: str = "yolo11m.pt"
    edge: EdgeConfig = Field(default_factory=EdgeConfig)
    cloud_sync: CloudSyncConfig = Field(default_factory=CloudSyncConfig)
    alerts: AlertConfig = Field(default_factory=AlertConfig)
    evidence: EvidenceConfig = Field(default_factory=EvidenceConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)
    recognition: RecognitionConfig = Field(default_factory=RecognitionConfig)
    features: FeatureConfig = Field(default_factory=FeatureConfig)
    object_security: ObjectSecurityConfig = Field(default_factory=ObjectSecurityConfig)
    cameras: list[CameraConfig] = Field(default_factory=list)

def _is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))

def _runtime_base() -> Path:
    configured = os.environ.get("CAMERA_AUTOMATION_HOME")
    if configured:
        return Path(configured)
    if _is_frozen():
        return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "MadhushalaCameraAI"
    return Path(".")

def _bundle_base() -> Path:
    if _is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(".")

def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}

def _local_cuda_available() -> bool:
    try:
        import torch
        return bool(torch.cuda.is_available())
    except Exception:
        return False

def _auto_runtime_profile() -> str:
    if os.environ.get("SNAPKEY_CLOUD_INFERENCE_URL") and os.environ.get("SNAPKEY_CLOUD_INFERENCE_TOKEN"):
        return "cloud_assist"
    if _local_cuda_available():
        return "performance"
    cpu_count = os.cpu_count() or 2
    if cpu_count <= 2:
        return "low_power"
    return "balanced"

def _selected_runtime_profile(config: AppConfig) -> RuntimeProfile:
    override = os.environ.get("SNAPKEY_RUNTIME_PROFILE", "").strip().lower()
    profile_name = override or config.runtime.profile
    if profile_name == "auto":
        profile_name = _auto_runtime_profile()
    return getattr(config.runtime, profile_name, config.runtime.balanced)

def _resolve_under_base(value: str, base: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        return str(path)
    return str(base / path)

def _resolve_model_path(value: str) -> str:
    model_path = Path(value)
    if model_path.is_absolute() and model_path.exists():
        return str(model_path)
    bundle_base = _bundle_base()
    candidates = []
    if not model_path.is_absolute():
        candidates.append(bundle_base / model_path)
    candidates.extend([
        bundle_base / "yolo26n.pt",
        bundle_base / "yolo11n.pt",
        bundle_base / "yolo11m.pt",
        Path("yolo26n.pt"),
        Path("yolo11n.pt"),
        Path("yolo11m.pt"),
    ])
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return str(model_path)

def _resolve_loaded_config(config: AppConfig) -> AppConfig:
    runtime_base = _runtime_base()
    runtime_base.mkdir(parents=True, exist_ok=True)
    config.database_path = _resolve_under_base(config.database_path, runtime_base)
    config.evidence_dir = _resolve_under_base(config.evidence_dir, runtime_base)
    config.object_security.model_storage_dir = _resolve_under_base(config.object_security.model_storage_dir, runtime_base)
    config.runtime.person_model.storage_dir = _resolve_under_base(config.runtime.person_model.storage_dir, runtime_base)
    config.edge.license_cache_path = _resolve_under_base(config.edge.license_cache_path, runtime_base)
    Path(config.database_path).parent.mkdir(parents=True, exist_ok=True)
    Path(config.evidence_dir).mkdir(parents=True, exist_ok=True)

    profile = _selected_runtime_profile(config)
    model_override = os.environ.get("SNAPKEY_PERSON_MODEL", "").strip()
    requested_model = model_override or profile.model or config.runtime.person_model.pytorch_artifact or config.yolo_model
    config.yolo_model = _resolve_model_path(requested_model)
    config.object_security.inference.imgsz = min(
        int(config.object_security.inference.imgsz or profile.object_security_imgsz),
        profile.object_security_imgsz,
    )
    config.recognition.known_recheck_seconds = max(
        float(config.recognition.known_recheck_seconds or profile.face_recheck_seconds),
        profile.face_recheck_seconds,
    )
    os.environ.setdefault("SNAPKEY_PROFILE_TRACKING_FPS", str(profile.tracking_fps))
    os.environ.setdefault("SNAPKEY_PROFILE_TRACKING_IMGSZ", str(profile.tracking_imgsz))
    os.environ.setdefault("SNAPKEY_PROFILE_TRACKING_QUALITY", str(profile.tracking_quality))
    os.environ.setdefault("SNAPKEY_PROFILE_TRACKING_MODE", profile.tracking_mode)
    os.environ.setdefault("SNAPKEY_PROFILE_OBJECT_SECURITY_IMGSZ", str(profile.object_security_imgsz))
    if "SNAPKEY_INFERENCE_BACKEND" not in os.environ:
        os.environ["SNAPKEY_INFERENCE_BACKEND"] = "CLOUD_GPU" if config.runtime.profile == "cloud_assist" else config.runtime.inference_backend
    if not _truthy(os.environ.get("SNAPKEY_CLOUD_INFERENCE_FORCE")):
        os.environ.setdefault("SNAPKEY_INFERENCE_ALLOW_FALLBACK", "1")
    return config

def runtime_config_path(path: str = "config.yaml") -> Path:
    configured_path = os.environ.get("CAMERA_AUTOMATION_CONFIG")
    if configured_path:
        return Path(configured_path)
    if _is_frozen():
        return _runtime_base() / "config.yaml"
    return Path(path)

def bundled_config_path(path: str = "config.yaml") -> Path:
    if _is_frozen():
        return _bundle_base() / path
    return Path(path)

def save_edge_activation(activation: dict, path: str = "config.yaml") -> Path:
    target = runtime_config_path(path)
    source = target if target.exists() else bundled_config_path(path)
    data: dict = {}
    if source.exists():
        with source.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}

    edge = data.setdefault("edge", {})
    identity = activation.get("edge") or {}
    edge.update({
        "edge_id": identity["edge_id"],
        "tenant_id": identity["tenant_id"],
        "company_code": identity.get("company_code"),
        "shop_id": identity["shop_id"],
        "site_id": identity["site_id"],
        "activation_required": True,
    })
    if activation.get("license_public_key"):
        edge["license_public_key"] = activation["license_public_key"]

    cloud_sync = data.setdefault("cloud_sync", {})
    cloud_sync.update({
        "enabled": True,
        "base_url": activation["cloud"]["base_url"],
        "api_token": activation["cloud"]["api_token"],
        "timeout_seconds": cloud_sync.get("timeout_seconds", 10),
        "batch_size": cloud_sync.get("batch_size", 50),
        "interval_seconds": cloud_sync.get("interval_seconds", 15),
    })

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False)
    return target

def load_config(path: str = "config.yaml") -> AppConfig:
    configured_path = os.environ.get("CAMERA_AUTOMATION_CONFIG")
    if configured_path:
        p = Path(configured_path)
    elif _is_frozen():
        runtime_config = _runtime_base() / "config.yaml"
        bundled_config = _bundle_base() / path
        bundled_example = _bundle_base() / "config.example.yaml"
        p = runtime_config if runtime_config.exists() else bundled_config
        if not p.exists() and bundled_example.exists():
            p = bundled_example
    else:
        p=Path(path)
    if not p.exists():
        return _resolve_loaded_config(AppConfig())
    with p.open("r",encoding="utf-8") as f:
        return _resolve_loaded_config(AppConfig.model_validate(yaml.safe_load(f) or {}))
