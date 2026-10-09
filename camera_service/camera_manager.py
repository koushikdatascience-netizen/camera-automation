from __future__ import annotations
from typing import Literal
from camera_service.orientation import rotate_frame
from contextlib import contextmanager
from collections import deque
import json, os, sqlite3, threading, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
import re
import cv2
import numpy as np
import time
import sys
import subprocess
import logging
from urllib.parse import urlsplit, urlunsplit
from pydantic import BaseModel, Field, model_validator
from enum import Enum
from camera_service.models import IdentitySeen
from camera_service.object_security.alerts import ObjectSecurityAlerter
from camera_service.domain import ResourceScope
from camera_service.inference import build_runtime_router, select_runtime_backend

logger = logging.getLogger(__name__)

class CameraRole(str, Enum):
    ENTRANCE_EXIT = "ENTRANCE_EXIT"
    GENERAL = "GENERAL"
    SECURITY = "SECURITY"
    SHOPLIFTING = "SHOPLIFTING"

class CameraState(str, Enum):
    STARTING = "STARTING"
    ONLINE = "ONLINE"
    DEGRADED = "DEGRADED"
    RECONNECTING = "RECONNECTING"
    OFFLINE = "OFFLINE"
    STOPPED = "STOPPED"

class CameraZone(str, Enum):
    INSIDE = "inside"
    OUTSIDE = "outside"

class CameraFeatures(BaseModel):
    attendance: bool = False
    face_recognition: bool = False
    unknown_detection: bool = False
    unknown_person_detection: bool = False
    shoplifting: bool = False
    shoplifting_detection: bool = False
    object_security: bool = False

    @property
    def unknown_enabled(self) -> bool:
        return self.unknown_detection or self.unknown_person_detection

    @property
    def shoplifting_enabled(self) -> bool:
        return self.shoplifting or self.shoplifting_detection

class CameraConfig(BaseModel):
    camera_id: str
    name: str
    source_type: str = "rtsp"
    rtsp_url: str
    enabled: bool = True
    camera_role: CameraRole = CameraRole.GENERAL
    camera_zone: CameraZone = CameraZone.INSIDE
    crowd_threshold: int = 10
    tracking_fps: float = 3.0
    tracking_imgsz: int = 384
    tracking_quality: int = 65
    rotation_degrees: Literal[0, 90, 180, 270] = 0
    attendance_active: bool = True
    tracking_mode: str = "detect"
    features: CameraFeatures = Field(default_factory=CameraFeatures)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    @model_validator(mode='after')
    def attendance_requires_tracks(self):
        if self.camera_role == CameraRole.ENTRANCE_EXIT:
            self.features.attendance = True
            self.features.face_recognition = True
            # Entrance/exit attendance and unknown-person security are independent
            # capabilities. Preserve the explicit feature choices from CRM/edge.
            self.tracking_mode = 'track'
        return self

class CameraStatus(BaseModel):
    camera_id: str
    name: str
    state: CameraState
    online: bool
    last_frame_at: Optional[str] = None
    capture_fps: float = 0.0
    ai_fps: float = 0.0
    frames_received: int = 0
    frames_dropped: int = 0
    reconnect_count: int = 0
    last_error: Optional[str] = None
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None

class CameraManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._tracking_models = {}
        self._inference_backends = {}
        self._runtime_routers = {}
        self._tracking_error = None
        self._stream_active_tracks = {}
        self._alert_last_sent = {}
        self._track_identity_cache = {}
        self._full_frame_face_cache = {}
        self._tracking_stream_state = {}
        self._live_frames = {}
        self._live_frame_lock = threading.RLock()
        self._attendance_line_detectors = {}
        self._unknown_zone_state = {}
        self._security_alerter = ObjectSecurityAlerter()
        self._init_db()

    @contextmanager
    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=30, check_same_thread=False)
        c.row_factory = sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def _init_db(self):
        with self._conn() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS cameras (
                id TEXT PRIMARY KEY,
                camera_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                source_type TEXT NOT NULL,
                rtsp_url TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                camera_role TEXT NOT NULL,
                camera_zone TEXT NOT NULL DEFAULT 'inside',
                crowd_threshold INTEGER NOT NULL DEFAULT 10,
                features_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS security_zones (
                id TEXT PRIMARY KEY,
                camera_id TEXT NOT NULL,
                name TEXT NOT NULL,
                x REAL NOT NULL,
                y REAL NOT NULL,
                width REAL NOT NULL,
                height REAL NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_security_zones_camera ON security_zones(camera_id);

            CREATE TABLE IF NOT EXISTS camera_detection_config (
                camera_id TEXT PRIMARY KEY,
                mode TEXT NOT NULL CHECK(mode IN ('FULL_FRAME','CUSTOM_ZONES')),
                version INTEGER NOT NULL DEFAULT 1,
                cloud_version INTEGER NOT NULL DEFAULT 0,
                sync_status TEXT NOT NULL DEFAULT 'LOCAL',
                local_override INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS camera_status (
                camera_id TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                online INTEGER NOT NULL DEFAULT 0,
                last_frame_at TEXT,
                capture_fps REAL DEFAULT 0.0,
                ai_fps REAL DEFAULT 0.0,
                frames_received INTEGER DEFAULT 0,
                frames_dropped INTEGER DEFAULT 0,
                reconnect_count INTEGER DEFAULT 0,
                last_error TEXT,
                frame_width INTEGER,
                frame_height INTEGER,
                FOREIGN KEY(camera_id) REFERENCES cameras(camera_id)
            );
            ''')
            self._ensure_column(c, 'cameras', 'camera_zone', "TEXT NOT NULL DEFAULT 'inside'")
            self._ensure_column(c, 'cameras', 'crowd_threshold', 'INTEGER NOT NULL DEFAULT 10')
            self._ensure_column(c, 'cameras', 'rotation_degrees', 'INTEGER NOT NULL DEFAULT 0')
            self._ensure_column(c, 'cameras', 'attendance_active', 'INTEGER NOT NULL DEFAULT 1')
            # Once an operator edits a camera on the edge, cloud assignment polling
            # must not continuously overwrite role/features/performance choices.
            # The cloud can still provision new cameras; explicit cloud commands remain
            # available for deliberate remote changes.
            self._ensure_column(c, 'cameras', 'local_override', 'INTEGER NOT NULL DEFAULT 0')
            self._ensure_column(c, 'cameras', 'tracking_fps', 'REAL NOT NULL DEFAULT 3.0')
            self._ensure_column(c, 'cameras', 'tracking_imgsz', 'INTEGER NOT NULL DEFAULT 384')
            self._ensure_column(c, 'cameras', 'tracking_quality', 'INTEGER NOT NULL DEFAULT 65')
            self._ensure_column(c, 'cameras', 'tracking_mode', "TEXT NOT NULL DEFAULT 'detect'")
            # Forward-compatible runtime metadata used by newer edge builds. Keeping
            # the migration here makes existing ProgramData databases safe to upgrade.
            self._ensure_column(c, 'camera_status', 'operating_json', "TEXT NOT NULL DEFAULT '{}'")

            self._ensure_column(c, 'camera_status', 'frame_width', 'INTEGER')
            self._ensure_column(c, 'camera_status', 'frame_height', 'INTEGER')

    def publish_live_ai_frame(self, camera_id: str, frame) -> None:
        """Keep one in-memory annotated frame for temporary remote WebRTC viewing."""
        if frame is None:
            return
        with self._live_frame_lock:
            self._live_frames[str(camera_id)] = {
                "frame": frame.copy(),
                "updated_at": time.monotonic(),
            }

    def get_live_ai_frame(self, camera_id: str, max_age_seconds: float = 3.0):
        """Return a copy of the latest annotated AI frame without reopening the camera."""
        with self._live_frame_lock:
            item = self._live_frames.get(str(camera_id))
            if not item or time.monotonic() - float(item.get("updated_at", 0.0)) > max_age_seconds:
                return None
            frame = item.get("frame")
            return frame.copy() if frame is not None else None

    def _ensure_column(self, conn, table: str, column: str, definition: str):
        existing = {row['name'] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def list_security_zones(self, camera_id: str):
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM security_zones WHERE camera_id=? ORDER BY created_at", (camera_id,)
            ).fetchall()
            return [dict(row) for row in rows]

    def get_detection_config(self, camera_id: str, unknown_enabled: bool = True) -> dict[str, Any]:
        zones = self.list_security_zones(camera_id)
        with self._conn() as c:
            row = c.execute("SELECT * FROM camera_detection_config WHERE camera_id=?", (camera_id,)).fetchone()
        if row:
            mode = row["mode"]
            version = int(row["version"])
            cloud_version = int(row["cloud_version"])
            sync_status = row["sync_status"]
            local_override = bool(row["local_override"])
        else:
            # Existing installations with saved zones retain their custom behavior;
            # cameras without zones get safe full-frame detection by default.
            mode = "CUSTOM_ZONES" if zones else "FULL_FRAME"
            version, cloud_version, sync_status, local_override = 0, 0, "DEFAULT", False
        full_frame = [{"id": "FULL_FRAME", "name": "Full frame", "x": 0.0, "y": 0.0,
                       "width": 1.0, "height": 1.0, "enabled": True}]
        effective_mode = "DISABLED" if (not unknown_enabled or (mode == "CUSTOM_ZONES" and not any(zone.get("enabled") for zone in zones))) else mode
        effective_zones = full_frame if effective_mode == "FULL_FRAME" else (
            [zone for zone in zones if zone.get("enabled")] if effective_mode == "CUSTOM_ZONES" else []
        )
        return {"camera_id": camera_id, "mode": mode, "version": version,
                "cloud_version": cloud_version, "sync_status": sync_status,
                "local_override": local_override, "zones": zones,
                "effective_mode": effective_mode, "effective_zones": effective_zones}

    def replace_detection_config(self, camera_id: str, mode: str, zones: list[dict[str, Any]],
                                 expected_version: int | None = None, *, source: str = "LOCAL",
                                 cloud_version: int | None = None) -> dict[str, Any]:
        mode = str(mode or "").strip().upper()
        if mode not in {"FULL_FRAME", "CUSTOM_ZONES"}:
            raise ValueError("mode must be FULL_FRAME or CUSTOM_ZONES")
        if mode == "FULL_FRAME" and zones:
            raise ValueError("FULL_FRAME mode does not accept custom zones")
        cleaned = []
        seen_ids = set()
        for zone in zones:
            zone_id = str(zone.get("id") or uuid.uuid4())
            if zone_id in seen_ids:
                raise ValueError("Zone IDs must be unique")
            seen_ids.add(zone_id)
            name = str(zone.get("name") or "Detection Zone").strip()
            if not name or len(name) > 80:
                raise ValueError("Zone name must contain 1 to 80 characters")
            x, y, width, height = [float(zone.get(key, 0)) for key in ("x", "y", "width", "height")]
            if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < width <= 1 and 0 < height <= 1):
                raise ValueError("Zone coordinates must be normalized to 0..1")
            if x + width > 1.000001 or y + height > 1.000001:
                raise ValueError("Zone must fit inside the camera frame")
            cleaned.append({"id": zone_id, "name": name, "x": x, "y": y, "width": width,
                            "height": height, "enabled": bool(zone.get("enabled", True))})
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._conn() as c:
            current = c.execute("SELECT * FROM camera_detection_config WHERE camera_id=?", (camera_id,)).fetchone()
            version = int(current["version"]) if current else 0
            if expected_version is not None and expected_version != version:
                raise RuntimeError(f"Detection configuration version conflict: expected {expected_version}, current {version}")
            existing_zones = [dict(row) for row in c.execute(
                "SELECT id,name,x,y,width,height,enabled FROM security_zones WHERE camera_id=? ORDER BY created_at,id",
                (camera_id,)).fetchall()]
            desired = sorted(cleaned, key=lambda item: item["id"])
            existing_norm = sorted([{**item, "enabled": bool(item["enabled"])} for item in existing_zones],
                                   key=lambda item: item["id"])
            same = current and current["mode"] == mode and existing_norm == desired
            next_version = version if same else version + 1
            if not same:
                # Replace only this canonical camera's zones. A full-frame switch
                # preserves its custom zone cache so users can switch back safely.
                if mode == "CUSTOM_ZONES":
                    c.execute("DELETE FROM security_zones WHERE camera_id=?", (camera_id,))
                    for zone in cleaned:
                        c.execute("""INSERT INTO security_zones(id,camera_id,name,x,y,width,height,enabled,created_at,updated_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?)""", (zone["id"],camera_id,zone["name"],zone["x"],zone["y"],
                            zone["width"],zone["height"],1 if zone["enabled"] else 0,now,now))
                c.execute("""INSERT INTO camera_detection_config(camera_id,mode,version,cloud_version,sync_status,local_override,updated_at)
                    VALUES(?,?,?,?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET mode=excluded.mode,
                    version=excluded.version,cloud_version=CASE WHEN ? IS NULL THEN camera_detection_config.cloud_version ELSE excluded.cloud_version END,
                    sync_status=excluded.sync_status,local_override=excluded.local_override,updated_at=excluded.updated_at""",
                    (camera_id,mode,next_version,int(cloud_version or 0),"SYNCED" if source == "CLOUD" else "LOCAL",
                     0 if source == "CLOUD" else 1,now,cloud_version))
            elif source == "CLOUD" and cloud_version is not None:
                c.execute("UPDATE camera_detection_config SET cloud_version=?,sync_status='SYNCED',local_override=0,updated_at=? WHERE camera_id=?",
                          (int(cloud_version),now,camera_id))
        return self.get_detection_config(camera_id)

    def apply_cloud_detection_config(self, camera_id: str, configuration: dict[str, Any]) -> dict[str, Any]:
        current = self.get_detection_config(camera_id)
        if current["local_override"]:
            return {**current, "sync_status": "LOCAL_OVERRIDE"}
        mode = str(configuration.get("mode") or "FULL_FRAME").upper()
        cloud_version = max(0, int(configuration.get("version") or 0))
        if cloud_version < current["cloud_version"]:
            return {**current, "sync_status": "STALE_CLOUD_CONFIG"}
        zones = configuration.get("zones") or []
        result = self.replace_detection_config(camera_id, mode, zones,
            source="CLOUD", cloud_version=cloud_version)
        return result

    def save_security_zone(self, camera_id: str, zone: Dict[str, Any]):
        name = str(zone.get("name") or "Detection Zone").strip()[:80]
        values = [float(zone.get(key, 0)) for key in ("x", "y", "width", "height")]
        x, y, width, height = values
        if not (0 <= x <= 1 and 0 <= y <= 1 and 0 < width <= 1 and 0 < height <= 1):
            raise ValueError("Zone coordinates must be normalized to 0..1")
        if x + width > 1.000001 or y + height > 1.000001:
            raise ValueError("Zone must fit inside the camera frame")
        zone_id = str(zone.get("id") or uuid.uuid4())
        zones = [item for item in self.list_security_zones(camera_id) if item["id"] != zone_id]
        zones.append({"id": zone_id, "name": name, "x": x, "y": y, "width": width,
                      "height": height, "enabled": bool(zone.get("enabled", True))})
        config = self.replace_detection_config(camera_id, "CUSTOM_ZONES", zones)
        return next(item for item in config["zones"] if item["id"] == zone_id)

    def delete_security_zone(self, camera_id: str, zone_id: str) -> bool:
        zones = self.list_security_zones(camera_id)
        if not any(item["id"] == zone_id for item in zones):
            return False
        self.replace_detection_config(camera_id, "CUSTOM_ZONES",
                                      [item for item in zones if item["id"] != zone_id])
        return True

    def _matching_security_zone(self, camera_id: str, bbox, frame_shape):
        detection = self.get_detection_config(camera_id, unknown_enabled=True)
        zones = detection["effective_zones"]
        if not zones:
            return None
        height, width = frame_shape[:2]
        x1, y1, x2, y2 = [float(v) for v in bbox]
        cx = ((x1 + x2) / 2.0) / max(1.0, float(width))
        cy = ((y1 + y2) / 2.0) / max(1.0, float(height))
        for zone in zones:
            if zone["x"] <= cx <= zone["x"] + zone["width"] and zone["y"] <= cy <= zone["y"] + zone["height"]:
                return zone
        return None

    def _confirmed_unknown_zone(self, camera_id: str, track_id: str, bbox, frame_shape, seconds: float = 2.0):
        zone = self._matching_security_zone(camera_id, bbox, frame_shape)
        key_prefix = f"{camera_id}:{track_id}:"
        now = time.monotonic()
        if not zone:
            for key in list(self._unknown_zone_state):
                if key.startswith(key_prefix):
                    self._unknown_zone_state.pop(key, None)
            return None
        key = key_prefix + str(zone["id"])
        for previous_key in list(self._unknown_zone_state):
            if previous_key.startswith(key_prefix) and previous_key != key:
                self._unknown_zone_state.pop(previous_key, None)
        state = self._unknown_zone_state.setdefault(key, {"first_seen": now, "last_seen": now})
        # A long recognition/camera gap breaks temporal confirmation. Do not let
        # one observation followed much later by another confirm an unknown.
        if now - float(state.get("last_seen", now)) > max(1.5, min(float(seconds), 3.0)):
            state["first_seen"] = now
        state["last_seen"] = now
        if now - state["first_seen"] >= max(0.5, float(seconds)):
            return zone
        return None

    def _unknown_confirmation_elapsed(self, camera_id: str, track_id: str, zone_id: str | None = None) -> float:
        prefix = f"{camera_id}:{track_id}:"
        now = time.monotonic()
        starts = [float(state.get("first_seen", now)) for key, state in self._unknown_zone_state.items()
                  if key == prefix + str(zone_id)] if zone_id is not None else [
                      float(state.get("first_seen", now)) for key, state in self._unknown_zone_state.items()
                      if key.startswith(prefix)]
        return max(0.0, now - min(starts)) if starts else 0.0

    @staticmethod
    def _log_unknown_decision(recognition_config, camera_id, track_id, reason, recognition_outcome,
                              confirmation_duration=0.0):
        if not bool(getattr(recognition_config, "unknown_detection_diagnostics", False)):
            return
        # Deliberately limited to identifiers and decision metadata. Never include
        # face embeddings, image paths, or image content in diagnostics.
        logger.info("unknown_detection_decision %s", json.dumps({
            "camera_id": str(camera_id),
            "track_id": str(track_id),
            "decision_reason": str(reason),
            "confirmation_duration_seconds": round(float(confirmation_duration), 3),
            "recognition_outcome": str(recognition_outcome),
        }, separators=(",", ":")))

    def _mask_rtsp_password(self, rtsp_url: str) -> str:
        """Mask password in RTSP URL for security"""
        if not rtsp_url:
            return rtsp_url
        if "://" in rtsp_url:
            parsed = urlsplit(rtsp_url)
            netloc = parsed.netloc.split('@')[-1]
            if parsed.username:
                netloc = f"{parsed.username}:*****@{netloc}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))
        # Pattern: rtsp://username:password@host:port/path
        pattern = r'rtsp://([^:]+):([^@]+)@'
        match = re.search(pattern, rtsp_url)
        if match:
            username = match.group(1)
            masked_password = '*****'
            return rtsp_url.replace(f'{username}:{match.group(2)}@', f'{username}:{masked_password}@')
        return rtsp_url

    def create_camera(self, camera_data: Dict[str, Any]) -> CameraConfig:
        """Create a new camera configuration"""
        camera_id = str(camera_data.get('camera_id', '')).strip()
        if not camera_id:
            raise ValueError("camera_id is required")
        camera_data = {**camera_data, 'camera_id': camera_id}
        if any(camera.rtsp_url == str(camera_data.get('rtsp_url','')).strip() for camera in self.list_cameras()):
            raise ValueError('Camera source is already saved; configure the existing camera')

        # Validate and create camera config
        features = camera_data.get('features', {})
        config = CameraConfig(
            camera_id=camera_id,
            name=str(camera_data['name']).strip(),
            source_type=str(camera_data.get('source_type', 'rtsp')).strip() or 'rtsp',
            rtsp_url=str(camera_data['rtsp_url']).strip(),
            enabled=camera_data.get('enabled', True),
            camera_role=camera_data.get('camera_role', CameraRole.GENERAL),
            camera_zone=camera_data.get('camera_zone', CameraZone.INSIDE),
            crowd_threshold=max(1, int(camera_data.get('crowd_threshold', 10) or 10)),
            tracking_fps=max(1.0, min(float(camera_data.get('tracking_fps', os.environ.get("SNAPKEY_PROFILE_TRACKING_FPS", 3.0)) or 3.0), 12.0)),
            tracking_imgsz=max(256, min(int(camera_data.get('tracking_imgsz', os.environ.get("SNAPKEY_PROFILE_TRACKING_IMGSZ", 384)) or 384), 640)),
            tracking_quality=max(35, min(int(camera_data.get('tracking_quality', os.environ.get("SNAPKEY_PROFILE_TRACKING_QUALITY", 65)) or 65), 95)),
            tracking_mode=str(camera_data.get('tracking_mode', os.environ.get("SNAPKEY_PROFILE_TRACKING_MODE", "detect")) or 'detect').strip().lower() if str(camera_data.get('tracking_mode', os.environ.get("SNAPKEY_PROFILE_TRACKING_MODE", "detect")) or 'detect').strip().lower() in {'detect','track'} else 'detect',
            rotation_degrees=camera_data.get('rotation_degrees', 0),
            attendance_active=bool(camera_data.get('attendance_active', True)),
            features=CameraFeatures(**features),
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat()
        )

        with self._lock, self._conn() as c:
            c.execute('''
                INSERT INTO cameras
                (id, camera_id, name, source_type, rtsp_url, enabled, camera_role,
                 camera_zone, crowd_threshold, tracking_fps, tracking_imgsz,
                 tracking_quality, tracking_mode, rotation_degrees, attendance_active, features_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(uuid.uuid4()),
                config.camera_id,
                config.name,
                config.source_type,
                config.rtsp_url,
                1 if config.enabled else 0,
                config.camera_role.value,
                config.camera_zone.value,
                config.crowd_threshold,
                config.tracking_fps,
                config.tracking_imgsz,
                config.tracking_quality,
                config.tracking_mode,
                config.rotation_degrees,
                1 if config.attendance_active else 0,
                json.dumps(config.features.model_dump()),
                config.created_at,
                config.updated_at
            ))
            c.execute("INSERT OR REPLACE INTO camera_status(camera_id,state,online) VALUES(?,?,0)",(config.camera_id,CameraState.STOPPED.value))

        return config

    def apply_cloud_camera(self, camera_data: Dict[str, Any]) -> CameraConfig:
        """Idempotently apply a cloud camera assignment to the local edge database."""
        camera_id = str(camera_data.get("camera_id", "")).strip()
        if not camera_id:
            raise ValueError("camera_id is required")
        source = str(camera_data.get("source") or camera_data.get("rtsp_url") or "").strip()
        if not source:
            raise ValueError(f"camera {camera_id} has no source")
        if source.startswith("edge-local:"):
            existing_local = self.get_camera(source.split(":", 1)[1].strip() or camera_id)
            if not existing_local:
                raise ValueError(f"camera {camera_id} uses an edge-local source but is not present locally")
            source = existing_local.rtsp_url
        settings = camera_data.get("settings") or {}
        features = camera_data.get("features") or {}
        supported_features = {
            "attendance", "face_recognition", "unknown_detection", "unknown_person_detection",
            "shoplifting", "shoplifting_detection", "object_security",
        }
        local_features = {key: bool(value) for key, value in features.items() if key in supported_features}
        if 'unknown_detection' in local_features:
            local_features['unknown_person_detection'] = local_features['unknown_detection']
        if 'shoplifting' in local_features:
            local_features['shoplifting_detection'] = local_features['shoplifting']
        payload = {
            "camera_id": camera_id,
            "name": camera_data.get("name") or camera_id,
            "source_type": camera_data.get("source_type") or "rtsp",
            "rtsp_url": source,
            "enabled": bool(camera_data.get("enabled", True)),
            "camera_role": camera_data.get("camera_role") or CameraRole.GENERAL.value,
            "camera_zone": self._normalize_cloud_zone(camera_data.get("camera_zone")),
            "crowd_threshold": camera_data.get("crowd_threshold", 10),
            "tracking_fps": settings.get("tracking_fps", os.environ.get("SNAPKEY_PROFILE_TRACKING_FPS", 3.0)),
            "tracking_imgsz": settings.get("tracking_imgsz", settings.get("max_frame_width", os.environ.get("SNAPKEY_PROFILE_TRACKING_IMGSZ", 384))),
            "tracking_quality": {"performance": 55, "balanced": 65, "high": 85}.get(str(settings.get("tracking_quality")), settings.get("tracking_quality", 65)),
            "tracking_mode": settings.get("tracking_mode", os.environ.get("SNAPKEY_PROFILE_TRACKING_MODE", "detect")),
            "features": local_features,
        }
        existing = self.get_camera(camera_id)
        detection_config = camera_data.get("detection_config")
        if existing:
            with self._conn() as c:
                row = c.execute('SELECT local_override FROM cameras WHERE camera_id = ?', (camera_id,)).fetchone()
            if row and bool(row['local_override']):
                if isinstance(detection_config, dict):
                    self.apply_cloud_detection_config(camera_id, detection_config)
                return existing
            payload['features'] = {**existing.features.model_dump(), **local_features}
            candidate = CameraConfig(**{**existing.model_dump(), **payload})
            if candidate.model_dump() == existing.model_dump():
                result = existing
            else:
                result = self.update_camera(camera_id, payload)
            if isinstance(detection_config, dict):
                self.apply_cloud_detection_config(camera_id, detection_config)
            return result
        if any(camera.rtsp_url == source for camera in self.list_cameras()):
            raise ValueError("Camera source is already saved; configure the existing camera")
        result = self.create_camera(payload)
        if isinstance(detection_config, dict):
            self.apply_cloud_detection_config(camera_id, detection_config)
        return result

    @staticmethod
    def _normalize_cloud_zone(value: Any) -> str:
        zone = str(value or "").strip().lower()
        if zone in {"outside", "outdoor", "external", "exterior"}:
            return CameraZone.OUTSIDE.value
        return CameraZone.INSIDE.value

    def get_camera(self, camera_id: str) -> Optional[CameraConfig]:
        """Get camera configuration by ID"""
        with self._conn() as c:
            row = c.execute('''
                SELECT * FROM cameras WHERE camera_id = ?
            ''', (camera_id,)).fetchone()

            if not row:
                return None

            return CameraConfig(
                camera_id=row['camera_id'],
                name=row['name'],
                source_type=row['source_type'],
                rtsp_url=row['rtsp_url'],
                enabled=bool(row['enabled']),
                camera_role=CameraRole(row['camera_role']),
                camera_zone=CameraZone(row['camera_zone']),
                crowd_threshold=int(row['crowd_threshold']),
                tracking_fps=float(row['tracking_fps']),
                tracking_imgsz=int(row['tracking_imgsz']),
                tracking_quality=int(row['tracking_quality']),
                tracking_mode=row['tracking_mode'] if row['tracking_mode'] in {'detect','track'} else 'detect',
                rotation_degrees=row['rotation_degrees'],
                attendance_active=bool(row['attendance_active']),
                features=CameraFeatures(**json.loads(row['features_json'])),
                created_at=row['created_at'],
                updated_at=row['updated_at']
            )

    def list_cameras(self) -> List[CameraConfig]:
        """List all cameras"""
        with self._conn() as c:
            rows = c.execute('SELECT * FROM cameras ORDER BY name').fetchall()
            return [
                CameraConfig(
                    camera_id=row['camera_id'],
                    name=row['name'],
                    source_type=row['source_type'],
                    rtsp_url=row['rtsp_url'],
                    enabled=bool(row['enabled']),
                    camera_role=CameraRole(row['camera_role']),
                    camera_zone=CameraZone(row['camera_zone']),
                    crowd_threshold=int(row['crowd_threshold']),
                    tracking_fps=float(row['tracking_fps']),
                    tracking_imgsz=int(row['tracking_imgsz']),
                    tracking_quality=int(row['tracking_quality']),
                    tracking_mode=row['tracking_mode'] if row['tracking_mode'] in {'detect','track'} else 'detect',
                    rotation_degrees=row['rotation_degrees'],
                    attendance_active=bool(row['attendance_active']),
                    features=CameraFeatures(**json.loads(row['features_json'])),
                    created_at=row['created_at'],
                    updated_at=row['updated_at']
                ) for row in rows
            ]

    def update_camera(self, camera_id: str, updates: Dict[str, Any], *, mark_local_override: bool = False) -> Optional[CameraConfig]:
        """Update camera configuration.

        mark_local_override is used for operator/API edits. Background cloud assignment
        polling uses the default False so it cannot claim local ownership.
        """
        with self._lock:
            camera = self.get_camera(camera_id)
            if not camera:
                return None

            # Apply updates
            if 'name' in updates:
                camera.name = str(updates['name']).strip()
            if 'source_type' in updates:
                camera.source_type = str(updates['source_type']).strip() or 'rtsp'
            if 'rtsp_url' in updates:
                camera.rtsp_url = str(updates['rtsp_url']).strip()
            if 'enabled' in updates:
                camera.enabled = updates['enabled']
            if 'camera_role' in updates:
                camera.camera_role = CameraRole(updates['camera_role'])
            if 'camera_zone' in updates:
                camera.camera_zone = CameraZone(updates['camera_zone'])
            if 'crowd_threshold' in updates:
                camera.crowd_threshold = max(1, int(updates['crowd_threshold']))
            if 'tracking_fps' in updates:
                camera.tracking_fps = max(1.0, min(float(updates['tracking_fps']), 12.0))
            if 'tracking_imgsz' in updates:
                camera.tracking_imgsz = max(256, min(int(updates['tracking_imgsz']), 640))
            if 'tracking_quality' in updates:
                camera.tracking_quality = max(35, min(int(updates['tracking_quality']), 95))
            if 'tracking_mode' in updates:
                mode = str(updates['tracking_mode']).strip().lower()
                camera.tracking_mode = mode if mode in {'detect','track'} else 'detect'
            if 'rotation_degrees' in updates:
                camera.rotation_degrees = updates['rotation_degrees']
            if 'attendance_active' in updates:
                camera.attendance_active = bool(updates['attendance_active'])
            if 'features' in updates:
                camera.features = CameraFeatures(**updates['features'])

            camera = CameraConfig.model_validate(camera.model_dump())
            camera.updated_at = datetime.now(timezone.utc).isoformat()

            with self._conn() as c:
                c.execute('''
                    UPDATE cameras
                    SET name = ?, source_type = ?, rtsp_url = ?, enabled = ?,
                        camera_role = ?, camera_zone = ?, crowd_threshold = ?,
                        tracking_fps = ?, tracking_imgsz = ?, tracking_quality = ?,
                        tracking_mode = ?, rotation_degrees = ?, attendance_active = ?,
                        features_json = ?, updated_at = ?,
                        local_override = CASE WHEN ? THEN 1 ELSE local_override END
                    WHERE camera_id = ?
                ''', (
                    camera.name,
                    camera.source_type,
                    camera.rtsp_url,
                    1 if camera.enabled else 0,
                    camera.camera_role.value,
                    camera.camera_zone.value,
                    camera.crowd_threshold,
                    camera.tracking_fps,
                    camera.tracking_imgsz,
                    camera.tracking_quality,
                    camera.tracking_mode,
                    camera.rotation_degrees,
                    1 if camera.attendance_active else 0,
                    json.dumps(camera.features.model_dump()),
                    camera.updated_at,
                    1 if mark_local_override else 0,
                    camera_id
                ))

            return camera

    def delete_camera(self, camera_id: str) -> bool:
        """Delete camera configuration"""
        with self._lock, self._conn() as c:
            cur = c.execute('DELETE FROM cameras WHERE camera_id = ?', (camera_id,))
            return cur.rowcount > 0

    def _open_video_capture(self, source: str):
        """Open RTSP/file sources normally, with Windows webcam backend fallbacks."""
        cap, _ = self._open_video_capture_with_diagnostics(source)
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        return cap

    def _is_dshow_source(self, source: str) -> bool:
        return str(source).strip().lower().startswith("dshow:")

    def _dshow_device_name(self, source: str) -> str:
        return str(source).strip().split(":", 1)[1].strip()

    def _read_dshow_snapshot_result(self, source: str) -> tuple[Optional[bytes], Optional[str]]:
        device_name = self._dshow_device_name(source)
        if not device_name:
            return None, "DirectShow device name is empty"

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "dshow",
            "-video_size",
            "640x480",
            "-i",
            f"video={device_name}",
            "-frames:v",
            "1",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        except FileNotFoundError:
            return None, "ffmpeg was not found in PATH"
        except subprocess.TimeoutExpired:
            return None, "ffmpeg timed out while reading the camera"
        except OSError as exc:
            return None, str(exc)

        if completed.returncode != 0 or not completed.stdout:
            error_text = completed.stderr.decode("utf-8", errors="replace").strip()
            return None, error_text or f"ffmpeg exited with code {completed.returncode}"
        return completed.stdout, None

    def _read_dshow_snapshot(self, source: str) -> Optional[bytes]:
        snapshot, _ = self._read_dshow_snapshot_result(source)
        return snapshot

    def _iter_dshow_mjpeg_frames(self, source: str, fps: int = 8):
        device_name = self._dshow_device_name(source)
        if not device_name:
            return

        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "dshow",
            "-video_size",
            "640x480",
            "-framerate",
            str(fps),
            "-i",
            f"video={device_name}",
            "-an",
            "-vf",
            f"fps={fps}",
            "-f",
            "image2pipe",
            "-vcodec",
            "mjpeg",
            "-q:v",
            "6",
            "-",
        ]
        process = None
        try:
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            if process.stdout is None:
                return

            buffer = bytearray()
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                buffer.extend(chunk)

                while True:
                    start = buffer.find(b"\xff\xd8")
                    end = buffer.find(b"\xff\xd9", start + 2)
                    if start < 0 or end < 0:
                        if start > 0:
                            del buffer[:start]
                        break

                    frame = bytes(buffer[start:end + 2])
                    del buffer[:end + 2]
                    yield frame
        finally:
            if process is not None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()

    def _save_event_snapshot(self, frame, camera_id: str, prefix: str) -> Optional[str]:
        try:
            root = Path(self.db_path).parent / "evidence" / camera_id
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{prefix}_{int(time.time() * 1000)}.jpg"
            if not cv2.imwrite(str(path), frame):
                return None
            return str(path)
        except Exception:
            return None

    def _begin_evidence_clip(self, stream_state: dict | None, event_id: str, camera_id: str,
                             event_kind: str, post_duration: float = 10.0,
                             prebuffer_count: int = 30):
        """Capture bounded evidence: ~10 seconds before and 10 seconds after an alert.

        The rolling buffer stores compressed JPEGs at 3 fps to keep per-camera RAM
        bounded while still preserving useful context before the triggering event.
        """
        if stream_state is None or stream_state.get("security_clip"):
            return False
        prebuffer = list(stream_state.get("evidence_buffer") or [])[-max(1,prebuffer_count):]
        stream_state["security_clip"] = {
            "alert_id": event_id,
            "camera_id": camera_id,
            "event_kind": event_kind,
            "started_at": time.monotonic(),
            "post_duration": max(1.0,min(30.0,float(post_duration))),
            "prebuffer": prebuffer,
            "frames": [],
            "last_sample_at": 0.0,
        }
        return True

    def _begin_security_clip(self, stream_state: dict | None, alert_id: str, camera_id: str):
        self._begin_evidence_clip(stream_state, alert_id, camera_id, "security")

    def _begin_unknown_clip(self, stream_state: dict | None, incident_id: str, camera_id: str):
        self._begin_evidence_clip(stream_state, incident_id, camera_id, "unknown")

    def _begin_attendance_evidence(self, stream_state: dict | None, event_id: str,
                                   camera_id: str, first_snapshot: str | None):
        if stream_state is None:
            return
        snapshots=[first_snapshot] if first_snapshot and Path(first_snapshot).is_file() else []
        clip_started=self._begin_evidence_clip(stream_state,event_id,camera_id,"attendance",
                                                post_duration=3.0,prebuffer_count=6)
        task={"event_id":event_id,"camera_id":camera_id,"started_at":time.monotonic(),
              "last_snapshot_at":time.monotonic(),"snapshot_paths":snapshots,
              "clip_started":clip_started,"clip_missing_reason":None if clip_started else "camera_clip_capture_busy"}
        stream_state.setdefault("attendance_evidence_tasks",{})[event_id]=task
        if not snapshots:
            task["snapshot_missing_reason"]="initial_snapshot_unavailable"

    def _record_attendance_evidence_frame(self, stream_state: dict | None, frame, store=None):
        if stream_state is None:
            return
        tasks=stream_state.get("attendance_evidence_tasks") or {}
        now=time.monotonic()
        for event_id,task in list(tasks.items()):
            if len(task["snapshot_paths"])<3 and now-task["last_snapshot_at"]>=0.5:
                path=self._save_event_snapshot(frame,task["camera_id"],"attendance")
                if path:
                    task["snapshot_paths"].append(path)
                task["last_snapshot_at"]=now
            if now-task["started_at"]<3.0:
                continue
            missing={}
            if len(task["snapshot_paths"])<3:
                missing["snapshots"]=task.get("snapshot_missing_reason") or f"capture_incomplete_{len(task['snapshot_paths'])}_of_3"
            if task.get("clip_missing_reason"):
                missing["clip"]=task["clip_missing_reason"]
            if task.get("clip_started") and (stream_state.get("security_clip") or {}).get("alert_id")==event_id:
                self._finalize_security_clip(stream_state,store)
            if store is not None and hasattr(store,"update_person_event_evidence"):
                store.update_person_event_evidence(event_id,snapshot_paths=task["snapshot_paths"],
                    evidence_missing=missing,evidence_pending=bool(task.get("clip_started")))
            tasks.pop(event_id,None)

    def _finish_attendance_evidence(self, stream_state: dict | None, store=None,
                                    reason: str = "camera_stopped"):
        if stream_state is None:
            return
        tasks=stream_state.get("attendance_evidence_tasks") or {}
        for event_id,task in list(tasks.items()):
            missing={}
            if len(task["snapshot_paths"])<3:
                missing["snapshots"]=f"{reason}_{len(task['snapshot_paths'])}_of_3"
            if task.get("clip_missing_reason"):
                missing["clip"]=task["clip_missing_reason"]
            elif task.get("clip_started"):
                missing["clip"]=f"{reason}_before_clip_complete"
            if task.get("clip_started") and (stream_state.get("security_clip") or {}).get("alert_id")==event_id:
                self._finalize_security_clip(stream_state,store)
            if store is not None and hasattr(store,"update_person_event_evidence"):
                store.update_person_event_evidence(event_id,snapshot_paths=task["snapshot_paths"],
                    evidence_missing=missing,evidence_pending=bool(task.get("clip_started")))
            tasks.pop(event_id,None)

    def _record_security_clip_frame(self, stream_state: dict | None, frame, store=None):
        if stream_state is None:
            return
        now = time.monotonic()
        # Keep only a compressed 10-second rolling history at ~3 fps.
        if now - stream_state.get("evidence_sample_at", 0.0) >= (1.0 / 3.0):
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 55])
            if ok:
                stream_state.setdefault("evidence_buffer", deque(maxlen=30)).append(encoded.tobytes())
                stream_state["evidence_sample_at"] = now
        clip = stream_state.get("security_clip")
        if not clip:
            return
        if now - clip.get("last_sample_at", 0.0) >= (1.0 / 3.0):
            ok, encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), 60])
            if ok:
                clip["frames"].append(encoded.tobytes())
                clip["last_sample_at"] = now
        if now - clip["started_at"] >= clip["post_duration"]:
            self._finalize_security_clip(stream_state, store)

    def _finalize_security_clip(self, stream_state: dict | None, store=None):
        if stream_state is None or not stream_state.get("security_clip"):
            return
        clip = stream_state.pop("security_clip")
        encoded_frames = (clip.get("prebuffer") or []) + (clip.get("frames") or [])
        if not encoded_frames:
            if clip.get("event_kind")=="attendance" and store is not None:
                store.update_person_event_evidence(clip["alert_id"],
                    evidence_missing={"clip":"no_frames"},evidence_pending=False)
            return
        thread = threading.Thread(target=self._write_security_clip, args=(clip, encoded_frames, store), daemon=True)
        thread.start()

    def _write_security_clip(self, clip: dict, encoded_frames: list, store=None):
        try:
            frames=[]
            for encoded in encoded_frames:
                frame=cv2.imdecode(np.frombuffer(encoded,dtype=np.uint8),cv2.IMREAD_COLOR)
                if frame is not None:
                    frames.append(frame)
            if not frames:
                if clip.get("event_kind")=="attendance" and store is not None:
                    store.update_person_event_evidence(clip["alert_id"],evidence_missing={"clip":"no_frames"},evidence_pending=False)
                return
            root = Path(self.db_path).parent / "evidence" / clip["camera_id"]
            root.mkdir(parents=True, exist_ok=True)
            prefix = {"unknown":"unknown_clip","attendance":"attendance_clip"}.get(clip.get("event_kind"),"security_clip")
            path = root / f"{prefix}_{int(time.time() * 1000)}.mp4"
            h, w = frames[0].shape[:2]
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 3.0, (w, h))
            if not writer.isOpened():
                if clip.get("event_kind")=="attendance" and store is not None:
                    store.update_person_event_evidence(clip["alert_id"],evidence_missing={"clip":"video_writer_unavailable"},evidence_pending=False)
                return
            for frame in frames:
                if frame.shape[:2] != (h, w):
                    frame = cv2.resize(frame, (w, h))
                writer.write(frame)
            writer.release()
            if store is not None:
                if clip.get("event_kind") == "unknown":
                    store.update_unknown_clip(clip["alert_id"], str(path))
                elif clip.get("event_kind")=="attendance":
                    store.update_person_event_evidence(clip["alert_id"],clip_path=str(path),evidence_pending=False)
                else:
                    store.update_security_alert_clip(clip["alert_id"], str(path))
        except Exception as exc:
            if clip.get("event_kind")=="attendance" and store is not None:
                try:
                    store.update_person_event_evidence(clip["alert_id"],
                        evidence_missing={"clip":f"capture_failed_{type(exc).__name__}"},evidence_pending=False)
                except Exception:
                    pass
            return

    def _prepare_tracking_frame(self, frame, camera_config=None):
        """Downscale large camera frames before AI/streaming for CPU-friendly demos."""
        profile_imgsz = int(os.environ.get("SNAPKEY_PROFILE_TRACKING_IMGSZ", "640") or 640)
        configured_imgsz = int(getattr(camera_config, "tracking_imgsz", profile_imgsz) or profile_imgsz)
        max_width = max(480, min(configured_imgsz, profile_imgsz) * 2, 960)
        h, w = frame.shape[:2]
        if w <= max_width:
            return frame
        scale = max_width / float(w)
        return cv2.resize(frame, (max_width, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)

    def _should_run_tracking_ai(self, stream_state: dict | None, camera_config=None) -> bool:
        if stream_state is None:
            return True
        now = time.monotonic()
        profile_fps = float(os.environ.get("SNAPKEY_PROFILE_TRACKING_FPS", "8") or 8)
        configured_fps = float(getattr(camera_config, "tracking_fps", profile_fps) or profile_fps)
        ai_fps = max(0.5, min(configured_fps, profile_fps, 8.0))
        if not stream_state.get("latest_summary"):
            stream_state["last_ai_started_at"] = now
            return True
        if now - stream_state.get("last_ai_started_at", 0.0) >= (1.0 / ai_fps):
            stream_state["last_ai_started_at"] = now
            return True
        return False

    def _should_emit_alert(self, key: str, cooldown_seconds: float = 30.0) -> bool:
        now = time.monotonic()
        last = self._alert_last_sent.get(key, 0)
        if now - last < cooldown_seconds:
            return False
        self._alert_last_sent[key] = now
        return True

    def _face_recheck_seconds(self, recognition_config) -> float:
        configured = float(getattr(recognition_config, "known_recheck_seconds", 2.0) or 2.0)
        return max(0.75, min(configured, 3.0))

    def _is_valid_security_detection(self, frame_shape, bbox, confidence: float, min_confidence: float) -> bool:
        if float(confidence) < float(min_confidence):
            return False
        h, w = frame_shape[:2]
        x1, y1, x2, y2 = [float(v) for v in bbox]
        box_w = max(0.0, x2 - x1)
        box_h = max(0.0, y2 - y1)
        if box_w < 12 or box_h < 12:
            return False
        area_ratio = (box_w * box_h) / max(1.0, float(w * h))
        return 0.00035 <= area_ratio <= 0.35

    def _annotate_tracking_frame(self, frame, model_path: str, face_service=None, recognition_config=None, camera_config=None, attendance_engine=None, store=None, stream_state=None, object_security_model_path: str | None = None, object_security_confidence: float = 0.55):
        try:
            overlays = []
            profile_imgsz = int(os.environ.get("SNAPKEY_PROFILE_TRACKING_IMGSZ", "384") or 384)
            imgsz = min(int(getattr(camera_config, "tracking_imgsz", profile_imgsz) or profile_imgsz), profile_imgsz)
            mode = getattr(camera_config, "tracking_mode", None) or os.environ.get("SNAPKEY_PROFILE_TRACKING_MODE", "detect") or "detect"
            router_setting = os.environ.get("SNAPKEY_INFERENCE_ROUTER_ENABLED", "1").strip().lower()
            use_router = router_setting not in {"0", "false", "no", "off"}

            if use_router:
                model_key = (model_path, getattr(camera_config, "camera_id", "preview"))
                backend = self._inference_backends.get(model_key)
                if backend is None:
                    router, capability = build_runtime_router(model_path)
                    backend = select_runtime_backend(router)
                    self._runtime_routers[model_key] = router
                    self._inference_backends[model_key] = backend
                    if stream_state is not None:
                        stream_state["inference_capability"] = capability
                camera_id_for_scope = getattr(camera_config, "camera_id", None) or "camera-unknown"
                scope = ResourceScope(
                    tenant_id=os.environ.get("SNAPKEY_TENANT_ID", "legacy-tenant"),
                    company_code=os.environ.get("SNAPKEY_COMPANY_CODE") or None,
                    shop_id=os.environ.get("SNAPKEY_SHOP_ID", "legacy-shop"),
                    edge_id=os.environ.get("SNAPKEY_EDGE_ID", "legacy-edge"),
                    camera_id=camera_id_for_scope,
                )
                inference_args = dict(scope=scope, frame_id=f"{camera_id_for_scope}-{time.time_ns()}",
                    tracking=(mode == "track"), imgsz=imgsz, conf=0.20, max_det=40)
                try:
                    normalized = backend.infer(frame, **inference_args)
                except Exception:
                    from camera_service.domain.inference import BackendType
                    router = self._runtime_routers.get(model_key)
                    fallback_allowed = os.environ.get('SNAPKEY_INFERENCE_ALLOW_FALLBACK','1').lower() not in {'0','false','no','off'}
                    if not fallback_allowed or router is None or getattr(backend,'backend_type',None) == BackendType.LOCAL_CPU:
                        raise
                    fallback = router.get(BackendType.LOCAL_CPU)
                    normalized = fallback.infer(frame, **inference_args)
                    self._inference_backends[model_key] = fallback
                    if stream_state is not None:
                        stream_state['fallback_reason'] = 'Remote/GPU inference failed; local CPU monitoring continues'
                names = {}
                class_ids = {}
                xyxy, confs, classes, track_ids = [], [], [], []
                for detection in normalized.detections:
                    class_id = int(detection.attributes.get("class_id", -1))
                    names[class_id] = detection.class_name
                    class_ids[detection.class_name] = class_id
                    xyxy.append(detection.bbox_xyxy)
                    confs.append(detection.confidence)
                    classes.append(class_id)
                    track_ids.append(detection.track_id)
                if stream_state is not None:
                    stream_state["inference_backend"] = normalized.backend_type.value
                    stream_state["inference_latency_ms"] = normalized.inference_latency_ms
            else:
                model_key = (model_path, getattr(camera_config, "camera_id", "preview"))
                model = self._tracking_models.get(model_key)
                if model is None:
                    from ultralytics import YOLO

                    model = YOLO(model_path)
                    self._tracking_models[model_key] = model

                try:
                    if mode == "track":
                        results = model.track(
                            frame,
                            persist=True,
                            tracker="bytetrack.yaml",
                            conf=0.20,
                            imgsz=imgsz,
                            max_det=40,
                            verbose=False,
                        )
                    else:
                        results = model.predict(
                            frame,
                            conf=0.20,
                            imgsz=imgsz,
                            max_det=40,
                            verbose=False,
                        )
                except Exception:
                    results = model.predict(
                        frame,
                        conf=0.20,
                        imgsz=imgsz,
                        max_det=40,
                        verbose=False,
                    )
                if not results:
                    if stream_state is not None:
                        stream_state["latest_overlays"] = []
                        stream_state["latest_summary"] = {
                            "people": 0,
                            "objects": 0,
                            "known": 0,
                            "unknown": 0,
                            "updated_at": time.monotonic(),
                            "error": None,
                        }
                    return frame

                result = results[0]
                names = getattr(result, "names", {}) or {}
                boxes = result.boxes
                if boxes is None:
                    if stream_state is not None:
                        stream_state["latest_overlays"] = []
                        stream_state["latest_summary"] = {
                            "people": 0,
                            "objects": 0,
                            "known": 0,
                            "unknown": 0,
                            "updated_at": time.monotonic(),
                            "error": None,
                        }
                    return frame

                xyxy = boxes.xyxy.cpu().numpy() if boxes.xyxy is not None else []
                confs = boxes.conf.cpu().tolist() if boxes.conf is not None else []
                classes = boxes.cls.int().cpu().tolist() if boxes.cls is not None else []
                track_ids = boxes.id.int().cpu().tolist() if boxes.id is not None else [None] * len(xyxy)
            active_known_tracks = set()
            now = time.monotonic()
            camera_id = camera_config.camera_id if camera_config is not None else None
            # Runtime role/attendance switches are persisted while the capture worker
            # remains alive. Refresh the lightweight camera config so Start/Stop
            # Attendance takes effect without reopening the webcam/RTSP source.
            runtime_camera_config = self.get_camera(camera_id) if camera_id else camera_config
            if runtime_camera_config is None:
                runtime_camera_config = camera_config
            camera_zone = runtime_camera_config.camera_zone.value if runtime_camera_config is not None else "inside"
            unknown_confirmation_seconds = max(
                0.5, float(getattr(recognition_config, "unknown_confirmation_seconds", 3.0) or 3.0)
            )
            crowd_threshold = camera_config.crowd_threshold if camera_config is not None else 10
            summary = {
                "people": 0,
                "objects": len(classes),
                "known": 0,
                "unknown": 0,
                "class_counts": {},
                "recognized_names": [],
                "feature_lines": [],
                "shoplifting_watch": None,
                "updated_at": time.monotonic(),
                "error": None,
            }
            enabled_features = []
            if camera_config is not None:
                feature_flags = camera_config.features.model_dump()
                enabled_features = [name.replace("_", " ").title() for name, enabled in feature_flags.items() if enabled]
                summary["feature_lines"] = enabled_features
            recognition_enabled = bool(
                face_service is not None
                and recognition_config is not None
                and getattr(recognition_config, "enabled", True)
                and camera_id is not None
            )
            face_recheck_seconds = self._face_recheck_seconds(recognition_config) if recognition_enabled else 2.0
            person_count = sum(1 for class_id in classes if names.get(class_id, f"class_{class_id}") == "person")
            summary["people"] = person_count
            for class_id in classes:
                class_name = names.get(class_id, f"class_{class_id}")
                summary["class_counts"][class_name] = summary["class_counts"].get(class_name, 0) + 1
            cv2.putText(frame, f"People: {person_count}", (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 3)
            cv2.putText(frame, f"People: {person_count}", (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 1)

            if camera_id and store and person_count > crowd_threshold and self._should_emit_alert(f"crowd:{camera_id}"):
                store.add_person_event(None, getattr(attendance_engine, "store_id", "store-1"), camera_id, "CROWD_ALERT", datetime.now(timezone.utc), {"person_count": person_count, "threshold": crowd_threshold, "camera_zone": camera_zone})
            if person_count > crowd_threshold:
                cv2.putText(frame, f"ALERT: crowd limit {person_count}/{crowd_threshold}", (20, 64), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 3)

            for coords, conf, class_id, track_id in zip(xyxy, confs, classes, track_ids):
                x1, y1, x2, y2 = [int(v) for v in coords]
                label = names.get(class_id, f"class_{class_id}")
                recognized_text = None
                confirmed_unknown_zone = None

                if label == "person" and track_id is not None and runtime_camera_config is not None:
                    unknown_role_allowed = runtime_camera_config.camera_role in {
                        CameraRole.SECURITY, CameraRole.ENTRANCE_EXIT
                    }
                    unknown_feature_enabled = runtime_camera_config.features.unknown_enabled
                    if unknown_role_allowed and unknown_feature_enabled and camera_zone == "inside":
                        # Zone presence is tracked at the camera AI frame cadence,
                        # independent of the slower face-recognition retry cadence.
                        confirmed_unknown_zone = self._confirmed_unknown_zone(
                            camera_id, str(track_id), (x1, y1, x2, y2), frame.shape,
                            seconds=unknown_confirmation_seconds,
                        )
                    elif runtime_camera_config.camera_role in {CameraRole.SECURITY, CameraRole.ENTRANCE_EXIT}:
                        reason = "unknown_detection_disabled" if not unknown_feature_enabled else "camera_zone_not_inside"
                        self._log_unknown_decision(recognition_config, camera_id, track_id, reason,
                                                   "not_attempted")
                        prefix = f"{camera_id}:{track_id}:"
                        for key in list(self._unknown_zone_state):
                            if key.startswith(prefix):
                                self._unknown_zone_state.pop(key, None)

                if (
                    label == "person"
                    and track_id is not None
                    and recognition_enabled
                    and attendance_engine is not None
                ):
                    cache_key = f"{camera_id}:{track_id}"
                    cached = self._track_identity_cache.get(cache_key)
                    should_check_face = True
                    if cached and now - cached.get("checked_at", 0) < face_recheck_seconds:
                        should_check_face = False
                        recognized_text = cached.get("text")
                        if cached.get("person_id"):
                            active_known_tracks.add(str(track_id))

                    roi = frame[max(0, y1):max(0, y2), max(0, x1):max(0, x2)]
                    if should_check_face and roi.size:
                        face_detection_failed = False
                        try:
                            faces = face_service.detect(roi)
                        except Exception:
                            faces = []
                            face_detection_failed = True
                            self._track_identity_cache.pop(cache_key, None)
                            if stream_state is not None:
                                stream_state["face_error"] = "Face recognition is unavailable"
                            self._log_unknown_decision(recognition_config, camera_id, track_id,
                                                       "face_detection_failed", "error")
                        if faces:
                            best = max(faces, key=lambda face: face_service.quality(face, roi.shape))
                            if best.get("embedding") is not None and face_service.quality(best, roi.shape) >= recognition_config.minimum_face_quality:
                                try:
                                    match, score = face_service.recognize(best.get("embedding"), recognition_config.known_threshold)
                                except Exception:
                                    match, score = None, 0.0
                                    face_detection_failed = True
                                    self._track_identity_cache.pop(cache_key, None)
                                    if stream_state is not None:
                                        stream_state["face_error"] = "Face recognition is unavailable"
                                    self._log_unknown_decision(recognition_config, camera_id, track_id,
                                                               "recognition_failed", "error")
                                if match:
                                    self._log_unknown_decision(recognition_config, camera_id, track_id,
                                                               "known_face_match", "known")
                                    active_known_tracks.add(str(track_id))
                                    snapshot_path = self._save_event_snapshot(frame, camera_id, "recognized")
                                    # Preserve the actual face that triggered recognition. The cloud
                                    # forwards this current camera image to CRM loginUsingFaceTenant;
                                    # a stored CRM profile image must never be substituted here.
                                    face_snapshot_path = None
                                    face_bbox = best.get("bbox")
                                    if face_bbox is not None and len(face_bbox) >= 4:
                                        fx1, fy1, fx2, fy2 = [int(v) for v in face_bbox[:4]]
                                        fx1=max(0,min(fx1,roi.shape[1]-1)); fx2=max(0,min(fx2,roi.shape[1]))
                                        fy1=max(0,min(fy1,roi.shape[0]-1)); fy2=max(0,min(fy2,roi.shape[0]))
                                        if fx2>fx1 and fy2>fy1:
                                            face_crop=roi[fy1:fy2,fx1:fx2]
                                            if face_crop.size:
                                                face_snapshot_path=self._save_event_snapshot(face_crop,camera_id,"recognized_face")
                                    recognized_name = match["full_name"]
                                    recognized_text = f"{recognized_name} {score:.2f}"
                                    if store and self._should_emit_alert(f"recognized:{camera_id}:{match['person_id']}", 5 if camera_config.features.attendance else 60):
                                        recognized_event_id=store.add_person_event(
                                            match["person_id"],getattr(attendance_engine,"store_id","store-1"),
                                            camera_id,"PERSON_RECOGNIZED",datetime.now(timezone.utc),
                                            {"track_id":str(track_id),"confidence":score,
                                             "snapshot_path":snapshot_path,"face_path":face_snapshot_path or snapshot_path,
                                             "snapshot_paths":[snapshot_path] if snapshot_path else [],
                                             "evidence_status":"PENDING_CAPTURE","evidence_pending":True})
                                        self._begin_attendance_evidence(stream_state,recognized_event_id,camera_id,snapshot_path)
                                    if runtime_camera_config.features.attendance and runtime_camera_config.attendance_active and track_id is not None:
                                        attendance_engine.on_identity(IdentitySeen(
                                            store_id=attendance_engine.store_id,
                                            camera_id=camera_id,
                                            track_id=str(track_id),
                                            person_id=match["person_id"],
                                            timestamp=datetime.now(timezone.utc),
                                            confidence=score,
                                            bbox=(float(x1), float(y1), float(x2), float(y2)),
                                            snapshot_path=snapshot_path,
                                        ))
                                    self._track_identity_cache[cache_key] = {
                                        "checked_at": now,
                                        "person_id": match["person_id"],
                                        "text": recognized_text,
                                        "name": recognized_name,
                                        "score": score,
                                    }
                                elif (not face_detection_failed and unknown_role_allowed
                                      and unknown_feature_enabled and camera_zone == "inside" and store):
                                    confirmation_duration = self._unknown_confirmation_elapsed(
                                        camera_id, str(track_id),
                                        confirmed_unknown_zone.get("id") if confirmed_unknown_zone else None,
                                    )
                                    should_create = bool(
                                        confirmed_unknown_zone
                                        and self._should_emit_alert(
                                            f"unknown:{camera_id}:{track_id}:{confirmed_unknown_zone['id']}", 20
                                        )
                                    )
                                    if should_create:
                                        event_time = datetime.now(timezone.utc)
                                        face_snapshot = self._save_event_snapshot(roi, camera_id, "unknown_face")
                                        person_snapshot = self._save_event_snapshot(frame, camera_id, "unknown_person")
                                        incident_id, created = store.upsert_unknown(
                                            getattr(attendance_engine, "store_id", "store-1"), camera_id, str(track_id),
                                            event_time, event_time, event_time, 1, score,
                                            face_path=face_snapshot, person_path=person_snapshot,
                                        )
                                        if created:
                                            self._log_unknown_decision(
                                                recognition_config, camera_id, track_id,
                                                "incident_created", "unmatched_face",
                                                confirmation_duration,
                                            )
                                            try:
                                                store.add_person_event(
                                                    None, getattr(attendance_engine, "store_id", "store-1"), camera_id,
                                                    "UNKNOWN_SECURITY_CONFIRMED", event_time,
                                                    {"track_id": str(track_id), "zone_id": confirmed_zone["id"], "zone_name": confirmed_zone["name"]},
                                                )
                                            except Exception:
                                                pass
                                            self._begin_unknown_clip(stream_state, incident_id, camera_id)
                                        else:
                                            self._log_unknown_decision(
                                                recognition_config, camera_id, track_id,
                                                "duplicate_open_incident", "unmatched_face",
                                                confirmation_duration,
                                            )
                                    if not face_detection_failed:
                                        self._track_identity_cache[cache_key] = {
                                            "checked_at": now,
                                            "person_id": None,
                                            "text": f"Unknown person {score:.2f}",
                                            "score": score,
                                        }
                                    if not should_create:
                                        reason = "zone_not_confirmed" if not confirmed_unknown_zone else "incident_cooldown"
                                        self._log_unknown_decision(
                                            recognition_config, camera_id, track_id, reason,
                                            "unmatched_face", confirmation_duration,
                                        )
                                elif not match and not face_detection_failed:
                                    reason = "unknown_detection_disabled" if not unknown_feature_enabled else "role_or_zone_not_allowed"
                                    self._log_unknown_decision(recognition_config, camera_id, track_id,
                                                               reason, "unmatched_face")
                            else:
                                self._track_identity_cache.pop(cache_key, None)
                                if best.get("embedding") is None:
                                    reason, outcome = "embedding_missing", "inconclusive"
                                else:
                                    reason, outcome = "face_quality_below_threshold", "inconclusive"
                                self._log_unknown_decision(recognition_config, camera_id, track_id,
                                                           reason, outcome)
                                self._track_identity_cache[cache_key] = {
                                    "checked_at": now,
                                    "person_id": None,
                                    "text": "Face too small/blurred",
                                    "score": 0.0,
                                }
                        elif not face_detection_failed:
                            self._track_identity_cache.pop(cache_key, None)
                            self._log_unknown_decision(recognition_config, camera_id, track_id,
                                                       "no_face_detected", "inconclusive")
                            self._track_identity_cache[cache_key] = {
                                "checked_at": now,
                                "person_id": None,
                                "text": None,
                                "score": 0.0,
                            }

                    cached_identity = self._track_identity_cache.get(cache_key) or {}
                    recognized_text = cached_identity.get('text')
                    if (recognized_text and recognized_text.lower().startswith('unknown')
                        and camera_zone == 'inside' and runtime_camera_config.features.unknown_enabled
                        and runtime_camera_config.camera_role in {CameraRole.SECURITY, CameraRole.ENTRANCE_EXIT}
                        and confirmed_unknown_zone):
                        self._security_alerter.alarm_beep(f'unknown:{camera_id}', True, 1250, 650, 8.0)

                track_text = f" ID {track_id}" if track_id is not None else ""
                text = recognized_text or f"{label}{track_text} {conf:.2f}"
                color = (0, 180, 255) if label == "person" else (40, 220, 80)
                if recognized_text:
                    if recognized_text.lower().startswith("unknown"):
                        summary["unknown"] += 1
                    elif not recognized_text.startswith("Face too small"):
                        summary["known"] += 1
                        cached_name = None
                        if label == "person" and track_id is not None and camera_id is not None:
                            cached_name = (self._track_identity_cache.get(f"{camera_id}:{track_id}") or {}).get("name")
                        summary["recognized_names"].append(cached_name or recognized_text.rsplit(" ", 1)[0])
                overlays.append({
                    "bbox": (x1, y1, x2, y2),
                    "text": text,
                    "color": color,
                })
                if (
                    label.lower() == "scissors"
                    and camera_config is not None
                    and camera_config.features.shoplifting_enabled
                    and store is not None
                ):
                    self._security_alerter.alarm_beep(f"tracking:{camera_id}:scissors", True, 1400, 160, 3.0)
                    if self._should_emit_alert(f"security_object:{camera_id}:scissors", 20):
                        snapshot_path = self._save_event_snapshot(frame, camera_id, "scissors_alert")
                        alert = store.create_security_alert(
                            getattr(attendance_engine, "store_id", "store-1"),
                            camera_id,
                            "SECURITY_OBJECT_ALERT",
                            "scissors",
                            float(conf),
                            datetime.now(timezone.utc),
                            snapshot_path=snapshot_path,
                            metadata={"bbox": [float(x1), float(y1), float(x2), float(y2)], "source": "tracking_stream"},
                        )
                        self._begin_security_clip(stream_state, alert["id"], camera_id)
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                cv2.rectangle(frame, (x1, max(0, y1 - 24)), (min(frame.shape[1], x1 + 220), y1), color, -1)
                cv2.putText(frame, text, (x1 + 4, max(16, y1 - 7)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)

            if (
                object_security_model_path
                and camera_config is not None
                and (camera_config.features.object_security or camera_config.features.shoplifting_enabled)
                and Path(object_security_model_path).exists()
            ):
                try:
                    valid_security_detections = []
                    security_key = (f"object-security:{object_security_model_path}", camera_id)
                    security_model = self._tracking_models.get(security_key)
                    if security_model is None:
                        from ultralytics import YOLO

                        security_model = YOLO(object_security_model_path)
                        self._tracking_models[security_key] = security_model
                    profile_security_imgsz = int(os.environ.get("SNAPKEY_PROFILE_OBJECT_SECURITY_IMGSZ", "640") or 640)
                    security_imgsz = max(416, min(int(getattr(camera_config, "tracking_imgsz", profile_security_imgsz) or profile_security_imgsz), profile_security_imgsz, 640))
                    security_results = security_model.predict(
                        frame,
                        conf=float(object_security_confidence),
                        imgsz=security_imgsz,
                        max_det=10,
                        verbose=False,
                    )
                    if security_results:
                        result = security_results[0]
                        security_names = getattr(result, "names", {}) or {}
                        security_boxes = result.boxes
                        if security_boxes is not None:
                            s_xyxy = security_boxes.xyxy.cpu().numpy() if security_boxes.xyxy is not None else []
                            s_confs = security_boxes.conf.cpu().tolist() if security_boxes.conf is not None else []
                            s_classes = security_boxes.cls.int().cpu().tolist() if security_boxes.cls is not None else []
                            for sec_idx, (coords, sec_conf, sec_class_id) in enumerate(zip(s_xyxy, s_confs, s_classes), start=1):
                                sec_label = str(security_names.get(sec_class_id, f"class_{sec_class_id}")).lower()
                                if sec_label != "scissors":
                                    continue
                                x1, y1, x2, y2 = [int(v) for v in coords]
                                if not self._is_valid_security_detection(frame.shape, (x1, y1, x2, y2), float(sec_conf), object_security_confidence):
                                    continue
                                valid_security_detections.append((x1, y1, x2, y2, float(sec_conf), sec_idx))

                    security_hits = int(stream_state.get("security_hits", 0)) if stream_state is not None else 0
                    if valid_security_detections:
                        security_hits = min(security_hits + 1, 10)
                    else:
                        security_hits = 0
                    if stream_state is not None:
                        stream_state["security_hits"] = security_hits

                    if security_hits >= 2:
                        for x1, y1, x2, y2, sec_conf, sec_idx in valid_security_detections:
                                text = f"scissors SEC-{sec_idx} {float(sec_conf):.2f}"
                                color = (0, 0, 255)
                                summary["objects"] += 1
                                summary["class_counts"]["scissors"] = summary["class_counts"].get("scissors", 0) + 1
                                overlays.append({
                                    "bbox": (x1, y1, x2, y2),
                                    "text": text,
                                    "color": color,
                                })
                                if (
                                    store is not None
                                    and camera_id is not None
                                ):
                                    self._security_alerter.alarm_beep(f"tracking:{camera_id}:scissors", True, 1400, 160, 3.0)
                                    if self._should_emit_alert(f"security_object:{camera_id}:scissors", 20):
                                        snapshot_path = self._save_event_snapshot(frame, camera_id, "scissors_alert")
                                        alert = store.create_security_alert(
                                            getattr(attendance_engine, "store_id", "store-1"),
                                            camera_id,
                                            "SECURITY_OBJECT_ALERT",
                                            "scissors",
                                            float(sec_conf),
                                            datetime.now(timezone.utc),
                                            snapshot_path=snapshot_path,
                                            metadata={"bbox": [float(x1), float(y1), float(x2), float(y2)], "source": "object_security_tracking"},
                                        )
                                        self._begin_security_clip(stream_state, alert["id"], camera_id)
                                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                                cv2.rectangle(frame, (x1, max(0, y1 - 24)), (min(frame.shape[1], x1 + 240), y1), color, -1)
                                cv2.putText(frame, text, (x1 + 4, max(16, y1 - 7)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 2)
                except Exception as exc:
                    summary["error"] = f"Object security: {str(exc)[:80]}"

            if camera_id is not None and attendance_engine is not None:
                previous = self._stream_active_tracks.get(camera_id, set())
                for lost_track in previous - active_known_tracks:
                    attendance_engine.on_track_lost(camera_id, lost_track)
                self._stream_active_tracks[camera_id] = active_known_tracks

            if recognition_enabled:
                try:
                    face_cache_key = camera_id or "default"
                    cached_faces = self._full_frame_face_cache.get(face_cache_key)
                    if not cached_faces or now - cached_faces.get("checked_at", 0) >= face_recheck_seconds:
                        face_overlays = []
                        for face in face_service.detect(frame):
                            x1, y1, x2, y2 = [int(v) for v in face["bbox"]]
                            match, score = face_service.recognize(
                                face.get("embedding"),
                                recognition_config.known_threshold,
                            )
                            if match:
                                face_overlays.append({
                                    "bbox": (x1, y1, x2, y2),
                                    "text": f"{match['full_name']} {score:.2f}",
                                    "color": (255, 180, 0),
                                })
                            else:
                                face_overlays.append({
                                    "bbox": (x1, y1, x2, y2),
                                    "text": f"Unknown face {score:.2f}",
                                    "color": (0, 0, 255),
                                })
                        self._full_frame_face_cache[face_cache_key] = {"checked_at": now, "overlays": face_overlays}
                    for overlay in self._full_frame_face_cache.get(face_cache_key, {}).get("overlays", []):
                        x1, y1, x2, y2 = overlay["bbox"]
                        text = overlay["text"]
                        color = overlay["color"]
                        overlays.append({
                            "bbox": (x1, y1, x2, y2),
                            "text": text,
                            "color": color,
                        })
                        cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
                        cv2.rectangle(frame, (x1, max(0, y1 - 24)), (min(frame.shape[1], x1 + 240), y1), color, -1)
                        cv2.putText(frame, text, (x1 + 4, max(16, y1 - 7)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)
                except Exception as exc:
                    cv2.putText(
                        frame,
                        f"Face overlay unavailable: {exc}",
                        (20, 70),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        0.65,
                        (0, 0, 255),
                        2,
                    )

            if camera_config is not None and camera_config.features.shoplifting_enabled:
                carry_item_names = {"backpack", "handbag", "suitcase", "bottle", "cell phone", "book", "umbrella"}
                carried = {
                    name: count
                    for name, count in summary["class_counts"].items()
                    if name in carry_item_names
                }
                if person_count and carried:
                    item_text = ", ".join(f"{name}:{count}" for name, count in carried.items())
                    summary["shoplifting_watch"] = f"Review person + item activity ({item_text})"
                    if store and self._should_emit_alert(f"shoplifting_watch:{camera_id}", 45):
                        store.add_person_event(None, getattr(attendance_engine, "store_id", "store-1"), camera_id, "SHOPLIFTING_WATCH", datetime.now(timezone.utc), {"person_count": person_count, "items": carried})
                elif person_count:
                    summary["shoplifting_watch"] = "Watching customer movement and item handling"
                else:
                    summary["shoplifting_watch"] = "Armed, waiting for people"

            if stream_state is not None:
                stream_state["latest_overlays"] = overlays
                stream_state["latest_summary"] = summary
                stream_state["last_ai_at"] = summary["updated_at"]
            return frame
        except Exception as exc:
            self._tracking_error = str(exc)
            if stream_state is not None:
                stream_state["latest_summary"] = {
                    "people": 0,
                    "objects": 0,
                    "known": 0,
                    "unknown": 0,
                    "updated_at": time.monotonic(),
                    "error": str(exc),
                }
            cv2.putText(
                frame,
                "Tracking temporarily unavailable",
                (20, 40),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.7,
                (0, 0, 255),
                2,
            )
            return frame

    def _draw_tracking_demo_overlay(self, frame, camera_config: CameraConfig, stream_state: dict, attendance_engine=None, store=None):
        overlays = stream_state.get("latest_overlays") or []
        summary = stream_state.get("latest_summary") or {}

        for overlay in overlays:
            x1, y1, x2, y2 = overlay["bbox"]
            color = tuple(overlay.get("color") or (0, 180, 255))
            text = overlay.get("text") or "detected"
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)
            label_width = max(120, min(frame.shape[1] - x1, 10 * len(text)))
            cv2.rectangle(frame, (x1, max(0, y1 - 24)), (min(frame.shape[1], x1 + label_width), y1), color, -1)
            cv2.putText(frame, text, (x1 + 4, max(16, y1 - 7)), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (0, 0, 0), 2)

        clock_text = datetime.now().strftime("%H:%M:%S")
        top_line = f"People: {summary.get('people', 0)}  Objects: {summary.get('objects', 0)}  {clock_text}"
        cv2.putText(frame, top_line, (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (255, 255, 255), 3)
        cv2.putText(frame, top_line, (20, 32), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (0, 0, 0), 1)
        return frame

    def _draw_tracking_activity_overlay(self, frame, stream_state: dict, attendance_engine=None, store=None):
        now = time.monotonic()
        if now - stream_state.get("activity_checked_at", 0.0) >= 1.0:
            lines = []
            try:
                people = {person["id"]: person for person in store.list_people()} if store is not None else {}
                presence = attendance_engine.presence_list() if attendance_engine is not None else []
                present = [
                    item for item in presence
                    if str(item.get("status", "")).upper() in {"PRESENT", "INSIDE", "ACTIVE", "ON_CAMERA"}
                ]
                breaks = [
                    item for item in presence
                    if item.get("break_started_at") or str(item.get("status", "")).upper().startswith("BREAK")
                ]
                lines.append(f"Attendance: {len(present)} present | {len(breaks)} on break")
                for item in presence[:2]:
                    person = people.get(item.get("person_id"), {})
                    name = person.get("full_name") or item.get("person_id") or "Unknown"
                    status = item.get("status") or "seen"
                    lines.append(f"{name}: {status}")

                if store is not None:
                    for event in store.person_events()[:2]:
                        name = event.get("full_name") or event.get("person_id") or "System"
                        lines.append(f"{event.get('event_type', 'EVENT')}: {name}")
            except Exception as exc:
                lines = [f"Attendance overlay unavailable: {str(exc)[:32]}"]
            stream_state["activity_lines"] = lines[:5]
            stream_state["activity_checked_at"] = now

        lines = stream_state.get("activity_lines") or ["Attendance: waiting for recognized people"]
        panel_w = min(470, frame.shape[1] - 20)
        panel_h = 34 + 24 * min(len(lines), 5)
        x0 = 10
        y0 = max(10, frame.shape[0] - panel_h - 10)
        overlay = frame.copy()
        cv2.rectangle(overlay, (x0, y0), (x0 + panel_w, y0 + panel_h), (0, 0, 0), -1)
        cv2.addWeighted(overlay, 0.55, frame, 0.45, 0, frame)
        cv2.putText(frame, "ATTENDANCE / BREAKS", (x0 + 12, y0 + 24), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (80, 220, 255), 2)
        for idx, line in enumerate(lines[:5]):
            cv2.putText(frame, line[:58], (x0 + 12, y0 + 50 + idx * 22), cv2.FONT_HERSHEY_SIMPLEX, 0.52, (255, 255, 255), 2)

    def _encode_tracking_frame(self, frame, model_path: str, face_service=None, recognition_config=None, camera_config=None, attendance_engine=None, store=None, stream_state=None, object_security_model_path: str | None = None, object_security_confidence: float = 0.55) -> Optional[bytes]:
        if self._should_run_tracking_ai(stream_state, camera_config):
            annotated = self._annotate_tracking_frame(frame, model_path, face_service, recognition_config, camera_config, attendance_engine, store, stream_state, object_security_model_path, object_security_confidence)
        else:
            annotated = self._draw_tracking_demo_overlay(frame, camera_config, stream_state or {}, attendance_engine, store)
        profile_quality = int(os.environ.get("SNAPKEY_PROFILE_TRACKING_QUALITY", "65") or 65)
        quality = max(35, min(int(getattr(camera_config, "tracking_quality", profile_quality) or profile_quality), profile_quality, 95))
        ok, encoded = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
        if not ok:
            return None
        return encoded.tobytes()

    def _encode_jpeg(self, frame) -> Optional[bytes]:
        ok, encoded = cv2.imencode(".jpg", frame)
        if not ok:
            return None
        return encoded.tobytes()

    def _open_video_capture_with_diagnostics(self, source: str):
        """Open a video source and return both the capture and backend attempts."""
        source_text = str(source).strip()
        attempts = []
        if source_text.isdigit():
            device_index = int(source_text)
            if sys.platform.startswith("win"):
                backends = [
                    ("DSHOW", getattr(cv2, "CAP_DSHOW", None)),
                    ("MSMF", getattr(cv2, "CAP_MSMF", None)),
                    ("DEFAULT", None),
                ]
                fallback = None
                for backend_name, backend in backends:
                    cap = (
                        cv2.VideoCapture(device_index)
                        if backend is None
                        else cv2.VideoCapture(device_index, backend)
                    )
                    if fallback is None:
                        fallback = cap
                    if not cap.isOpened():
                        attempts.append({
                            "backend": backend_name,
                            "opened": False,
                            "readable": False,
                            "message": "backend did not open source",
                        })
                        if cap is not fallback:
                            cap.release()
                        continue
                    for _ in range(5):
                        ok, frame = cap.read()
                        if ok and frame is not None:
                            attempts.append({
                                "backend": backend_name,
                                "opened": True,
                                "readable": True,
                                "frame_shape": list(frame.shape),
                            })
                            if fallback is not cap and fallback is not None:
                                fallback.release()
                            return cap, attempts
                        time.sleep(0.05)
                    attempts.append({
                        "backend": backend_name,
                        "opened": True,
                        "readable": False,
                        "message": "backend opened source but returned no frames",
                    })
                    if cap is not fallback:
                        cap.release()
                return fallback or cv2.VideoCapture(device_index), attempts
            return cv2.VideoCapture(device_index), attempts
        if source_text.lower().startswith(("rtsp://", "http://", "https://")):
            cap = cv2.VideoCapture(source_text, cv2.CAP_FFMPEG, [
                cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 3000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 3000])
        else:
            cap = cv2.VideoCapture(source_text)
        attempts.append({
            "backend": "DEFAULT",
            "opened": bool(cap.isOpened()),
            "readable": None,
            "message": "non-numeric source",
        })
        return cap, attempts

    def test_rtsp_connection(self, rtsp_url: str, timeout: int = 5) -> Dict[str, Any]:
        """Test RTSP/webcam connection and return diagnostics."""
        source_text = str(rtsp_url).strip()
        result = {
            'success': False,
            'message': '',
            'source': self._mask_rtsp_password(source_text),
            'attempts': [],
            'resolution': None,
            'fps': 0,
            'connection_ms': 0,
            'frames_received': 0
        }

        start_time = time.time()
        cap = None

        try:
            if self._is_dshow_source(source_text):
                snapshot, dshow_error = self._read_dshow_snapshot_result(source_text)
                if not snapshot:
                    result['message'] = 'Unable to read DirectShow camera through ffmpeg'
                    result['attempts'] = [{
                        "backend": "FFMPEG_DSHOW",
                        "opened": False,
                        "readable": False,
                        "message": dshow_error or "ffmpeg could not capture a frame",
                    }]
                    return result

                image = cv2.imdecode(np.frombuffer(snapshot, dtype=np.uint8), cv2.IMREAD_COLOR)
                if image is None:
                    result['message'] = 'DirectShow camera returned an invalid frame'
                    return result
                h, w = image.shape[:2]
                result.update({
                    'success': True,
                    'message': 'Camera connected successfully via ffmpeg DirectShow',
                    'attempts': [{
                        "backend": "FFMPEG_DSHOW",
                        "opened": True,
                        "readable": True,
                        "frame_shape": list(image.shape),
                    }],
                    'resolution': {"width": w, "height": h},
                    'fps': 1,
                    'connection_ms': round((time.time() - start_time) * 1000, 2),
                    'frames_received': 1,
                })
                return result

            cap, attempts = self._open_video_capture_with_diagnostics(source_text)
            result['attempts'] = attempts

            if not cap.isOpened():
                result['message'] = 'Unable to open stream'
                return result

            # Wait for connection to establish
            time.sleep(1)

            if not cap.isOpened():
                result['message'] = 'Connection failed after initialization'
                return result

            # Try to read a few frames
            frames_read = 0
            frame_times = []
            resolutions = []

            for _ in range(10):  # Try to read up to 10 frames
                ret, frame = cap.read()
                if not ret:
                    break

                frames_read += 1
                frame_times.append(time.time())

                if frame is not None:
                    h, w = frame.shape[:2]
                    resolutions.append((w, h))

                # Small delay to avoid overwhelming the stream
                time.sleep(0.05)

            cap.release()

            if frames_read == 0:
                result['message'] = 'No frames received'
                return result

            # Calculate metrics
            connection_time = (time.time() - start_time) * 1000
            if len(frame_times) > 1:
                frame_intervals = [frame_times[i+1] - frame_times[i] for i in range(len(frame_times)-1)]
                avg_interval = sum(frame_intervals) / len(frame_intervals)
                fps = 1.0 / avg_interval if avg_interval > 0 else 0
            else:
                fps = 0

            result.update({
                'success': True,
                'message': 'Camera connected successfully',
                'resolution': (
                    {"width": resolutions[-1][0], "height": resolutions[-1][1]}
                    if resolutions
                    else None
                ),
                'fps': round(fps, 2),
                'connection_ms': round(connection_time, 2),
                'frames_received': frames_read
            })

        except Exception as e:
            result['message'] = 'Camera connection failed'
        finally:
            if cap is not None:
                cap.release()

        return result

    def get_camera_status(self, camera_id: str) -> Optional[CameraStatus]:
        """Get current camera status"""
        with self._conn() as c:
            row = c.execute('''
                SELECT * FROM camera_status WHERE camera_id = ?
            ''', (camera_id,)).fetchone()

            if not row:
                return None

            status = CameraStatus(
                camera_id=row['camera_id'],
                name="",  # Will be populated from camera config
                state=CameraState(row['state']),
                online=bool(row['online']),
                last_frame_at=row['last_frame_at'],
                capture_fps=row['capture_fps'],
                ai_fps=row['ai_fps'],
                frames_received=row['frames_received'],
                frames_dropped=row['frames_dropped'],
                reconnect_count=row['reconnect_count'],
                last_error=row['last_error'],
                frame_width=row['frame_width'], frame_height=row['frame_height']
            )
            if status.online:
                stamp = datetime.fromisoformat(status.last_frame_at) if status.last_frame_at else None
                if stamp is None or (datetime.now(timezone.utc) - stamp).total_seconds() > 10:
                    status.online = False
                    status.state = CameraState.OFFLINE
            return status

    def update_camera_status(self, camera_id: str, status: CameraStatus | None = None, **changes):
        """Update camera runtime status from probes, streams, or background workers."""
        if status is None:
            current = self.get_camera_status(camera_id)
            camera = self.get_camera(camera_id)
            status = current or CameraStatus(
                camera_id=camera_id,
                name=camera.name if camera else "",
                state=CameraState.STOPPED,
                online=False,
            )
            data=status.model_dump()
            data.update(changes)
            data["camera_id"]=camera_id
            state=data.get("state")
            if isinstance(state,str):
                data["state"]=CameraState(state)
            status=CameraStatus(**data)
        with self._lock, self._conn() as c:
            c.execute('''
                INSERT OR REPLACE INTO camera_status
                (camera_id, state, online, last_frame_at, capture_fps, ai_fps,
                 frames_received, frames_dropped, reconnect_count, last_error,frame_width,frame_height)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                status.camera_id,
                status.state.value,
                1 if status.online else 0,
                status.last_frame_at,
                status.capture_fps,
                status.ai_fps,
                status.frames_received,
                status.frames_dropped,
                status.reconnect_count,
                status.last_error,
                status.frame_width,status.frame_height
            ))

    def delete_camera_status(self, camera_id: str) -> None:
        """Delete runtime status for a camera."""
        with self._lock, self._conn() as c:
            c.execute('DELETE FROM camera_status WHERE camera_id = ?', (camera_id,))

    def get_camera_snapshot(self, camera_id: str) -> Optional[bytes]:
        """Read a fresh JPEG snapshot from a saved camera RTSP URL."""
        camera = self.get_camera(camera_id)
        if not camera:
            return None
        return self.read_rtsp_snapshot(camera.rtsp_url)

    def read_rtsp_snapshot(self, rtsp_url: str) -> Optional[bytes]:
        """Open an RTSP URL or webcam index, read one frame, and return it as JPEG bytes."""
        if self._is_dshow_source(rtsp_url):
            return self._read_dshow_snapshot(rtsp_url)

        cap = self._open_video_capture(rtsp_url)
        try:
            if not cap.isOpened():
                return None

            frame = None
            for _ in range(10):
                ok, candidate = cap.read()
                if ok and candidate is not None:
                    frame = candidate
                    break
                time.sleep(0.05)

            if frame is None:
                return None

            ok, encoded = cv2.imencode(".jpg", frame)
            if not ok:
                return None
            return encoded.tobytes()
        finally:
            cap.release()

    def iter_rtsp_mjpeg(self, rtsp_url: str):
        """Yield MJPEG frames from an RTSP URL or webcam index for browser preview."""
        if self._is_dshow_source(rtsp_url):
            for snapshot in self._iter_dshow_mjpeg_frames(rtsp_url, fps=10):
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + snapshot
                    + b"\r\n"
                )
            return

        cap = self._open_video_capture(rtsp_url)
        try:
            while cap.isOpened():
                ok, frame = cap.read()
                if not ok or frame is None:
                    break
                ok, encoded = cv2.imencode(".jpg", frame)
                if not ok:
                    continue
                yield (
                    b"--frame\r\n"
                    b"Content-Type: image/jpeg\r\n\r\n"
                    + encoded.tobytes()
                    + b"\r\n"
                )
                time.sleep(0.03)
        finally:
            cap.release()

    def iter_tracking_mjpeg(self, camera_config: CameraConfig, model_path: str, face_service=None, recognition_config=None, attendance_engine=None, store=None, object_security_model_path: str | None = None, object_security_confidence: float = 0.55, stop_event=None, publish_callback=None):
        """Yield MJPEG frames from a latest-frame pipeline.

        Capture, AI, and browser streaming run independently so slow CPU
        inference cannot freeze the client-facing live view.
        """
        rtsp_url = camera_config.rtsp_url
        profile_fps = float(os.environ.get("SNAPKEY_PROFILE_TRACKING_FPS", "8") or 8)
        configured_fps = float(getattr(camera_config, "tracking_fps", profile_fps) or profile_fps)
        target_ai_fps = max(0.5, min(configured_fps, profile_fps, 8.0))
        display_fps = max(6.0, min(target_ai_fps * 4.0, 12.0))
        frame_delay = 1.0 / display_fps
        stream_state = {
            "security_clip": None,
            "attendance_evidence_tasks": {},
            "evidence_buffer": deque(maxlen=30),
            "evidence_sample_at": 0.0,
            "latest_overlays": [],
            "latest_summary": {
                "people": 0,
                "objects": 0,
                "known": 0,
                "unknown": 0,
                "updated_at": time.monotonic(),
                "error": None,
            },
        }
        stop_event = stop_event or threading.Event()
        frame_lock = threading.Lock()
        result_lock = threading.Lock()
        capture_state = {
            "latest_frame": None,
            "latest_seq": 0,
            "ai_seq": 0,
            "stream_seq": 0,
            "frames_received": 0,
            "frames_dropped": 0,
            "last_frame_at": None,
            "ai_frames": 0,
            "stopped": False,
            "error": None,
        }
        result_state = {
            "annotated_frame": None,
            "updated_at": 0.0,
            "error": None,
        }

        def publish_frame(frame):
            prepared = self._prepare_tracking_frame(rotate_frame(frame, camera_config.rotation_degrees), camera_config)
            with frame_lock:
                if capture_state["latest_seq"] > capture_state["ai_seq"]:
                    capture_state["frames_dropped"] += 1
                capture_state["latest_frame"] = prepared
                capture_state["latest_seq"] += 1
                capture_state["frames_received"] += 1
                capture_state["last_frame_at"] = datetime.now(timezone.utc).isoformat()

        def capture_worker():
            try:
                if self._is_dshow_source(rtsp_url):
                    for snapshot in self._iter_dshow_mjpeg_frames(rtsp_url, fps=max(1, int(display_fps))):
                        if stop_event.is_set():
                            break
                        frame = cv2.imdecode(np.frombuffer(snapshot, dtype=np.uint8), cv2.IMREAD_COLOR)
                        if frame is not None:
                            publish_frame(frame)
                    return

                cap = self._open_video_capture(rtsp_url)
                try:
                    while not stop_event.is_set() and cap.isOpened():
                        ok, frame = cap.read()
                        if not ok or frame is None:
                            break
                        publish_frame(frame)
                        if camera_config.source_type == "file":
                            stop_event.wait(1.0 / display_fps)
                finally:
                    cap.release()
            except Exception as exc:
                capture_state["error"] = f"Camera capture failed ({type(exc).__name__})"
            finally:
                capture_state["stopped"] = True

        def ai_worker():
            next_run_at = 0.0
            while not stop_event.is_set():
                now = time.monotonic()
                if now < next_run_at:
                    time.sleep(min(0.02, next_run_at - now))
                    continue

                with frame_lock:
                    frame = capture_state["latest_frame"]
                    seq = capture_state["latest_seq"]
                    if frame is None or seq == capture_state["ai_seq"]:
                        frame = None
                    else:
                        capture_state["ai_seq"] = seq

                if frame is None:
                    if capture_state["stopped"]:
                        break
                    time.sleep(0.02)
                    continue

                try:
                    ai_frame = frame.copy()
                    self._annotate_tracking_frame(
                        ai_frame,
                        model_path,
                        face_service,
                        recognition_config,
                        camera_config,
                        attendance_engine,
                        store,
                        stream_state,
                        object_security_model_path,
                        object_security_confidence,
                    )
                    with result_lock:
                        capture_state["ai_frames"] += 1
                        result_state["annotated_frame"] = ai_frame.copy()
                        result_state["updated_at"] = time.monotonic()
                        result_state["error"] = stream_state.get("latest_summary", {}).get("error")
                except Exception as exc:
                    with result_lock:
                        result_state["error"] = str(exc)
                    stream_state["latest_summary"] = {
                        "people": 0,
                        "objects": 0,
                        "known": 0,
                        "unknown": 0,
                        "updated_at": time.monotonic(),
                        "error": str(exc),
                    }
                next_run_at = time.monotonic() + (1.0 / target_ai_fps)

        capture_thread = threading.Thread(target=capture_worker, name=f"capture-{camera_config.camera_id}", daemon=True)
        ai_thread = threading.Thread(target=ai_worker, name=f"ai-{camera_config.camera_id}", daemon=True)
        capture_thread.start()
        ai_thread.start()
        runtime_started = time.monotonic()
        last_status_at = 0.0

        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline and not stop_event.is_set():
                with frame_lock:
                    if capture_state["latest_frame"] is not None:
                        break
                    stopped = capture_state["stopped"]
                    error = capture_state["error"]
                if stopped:
                    if error:
                        self._tracking_error = error
                    return
                time.sleep(0.03)

            try:
                while not stop_event.is_set():
                    started_at = time.monotonic()
                    with frame_lock:
                        frame = capture_state["latest_frame"]
                        if frame is not None:
                            frame = frame.copy()
                            capture_state["stream_seq"] = capture_state["latest_seq"]
                        stopped = capture_state["stopped"]
                        error = capture_state["error"]
                    if frame is None:
                        if stopped:
                            if error:
                                self._tracking_error = error
                            break
                        time.sleep(0.03)
                        continue

                    with result_lock:
                        cached_annotated = result_state["annotated_frame"]
                        cached_age = time.monotonic() - result_state["updated_at"] if result_state["updated_at"] else 999.0
                        ai_error = result_state["error"]
                    if stopped:
                        break
                    if time.monotonic() - last_status_at >= 1:
                        elapsed_runtime = max(1, time.monotonic() - runtime_started)
                        self.update_camera_status(camera_config.camera_id, state="DEGRADED" if ai_error else "ONLINE",
                            online=True, last_frame_at=capture_state["last_frame_at"],
                            capture_fps=capture_state["frames_received"]/elapsed_runtime,
                            ai_fps=capture_state["ai_frames"]/elapsed_runtime,
                            frames_received=capture_state["frames_received"], frames_dropped=capture_state["frames_dropped"],
                            last_error="AI inference unavailable" if ai_error else stream_state.get("face_error"))
                        last_status_at = time.monotonic()

                    if cached_age > 3.0:
                        stream_state["latest_overlays"] = []
                    if stream_state.get("latest_overlays"):
                        annotated = self._draw_tracking_demo_overlay(frame, camera_config, stream_state, attendance_engine, store)
                    elif cached_annotated is not None and cached_age <= 3.0:
                        annotated = cached_annotated.copy()
                    else:
                        annotated = self._draw_tracking_demo_overlay(frame, camera_config, stream_state, attendance_engine, store)
                        if ai_error:
                            cv2.putText(
                                annotated,
                                "AI temporarily unavailable",
                                (20, max(56, min(annotated.shape[0] - 20, 62))),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.55,
                                (0, 0, 255),
                                2,
                            )
                    # The same annotated frame shown by the local AI workspace is made
                    # available in memory to the optional WebRTC publisher. This avoids a
                    # second RTSP/webcam connection and keeps inference on the edge.
                    self.publish_live_ai_frame(camera_config.camera_id, annotated)
                    self._record_security_clip_frame(stream_state, frame, store)
                    self._record_attendance_evidence_frame(stream_state,frame,store)
                    profile_quality = int(os.environ.get("SNAPKEY_PROFILE_TRACKING_QUALITY", "65") or 65)
                    quality = max(35, min(int(getattr(camera_config, "tracking_quality", profile_quality) or profile_quality), profile_quality, 95))
                    ok, encoded = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                    if ok and encoded is not None:
                        if publish_callback:
                            raw_ok, raw_encoded = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                            if raw_ok:
                                publish_callback(raw_encoded.tobytes(), encoded.tobytes())
                        yield (
                            b"--frame\r\n"
                            b"Content-Type: image/jpeg\r\n\r\n"
                            + encoded.tobytes()
                            + b"\r\n"
                        )
                    elapsed = time.monotonic() - started_at
                    if elapsed < frame_delay:
                        time.sleep(frame_delay - elapsed)
            finally:
                stop_event.set()
        finally:
            stop_event.set()
            self._finish_attendance_evidence(stream_state,store,"camera_stopped")
            self._finalize_security_clip(stream_state, store)
            current_thread = threading.current_thread()
            if capture_thread is not current_thread:
                capture_thread.join(timeout=1.0)
            if ai_thread is not current_thread:
                ai_thread.join()
            for cache in (self._tracking_models, self._inference_backends, self._runtime_routers):
                for key in list(cache):
                    if isinstance(key, tuple) and key[1] == camera_config.camera_id:
                        cache.pop(key, None)
            for cache in (self._track_identity_cache, self._attendance_line_detectors):
                for key in list(cache):
                    if key.startswith(camera_config.camera_id + ':'):
                        cache.pop(key, None)
            self._full_frame_face_cache.pop(camera_config.camera_id, None)
            for track_id in self._stream_active_tracks.pop(camera_config.camera_id, set()):
                if attendance_engine is not None:
                    attendance_engine.on_track_lost(camera_config.camera_id, track_id)
