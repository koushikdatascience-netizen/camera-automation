from __future__ import annotations
from contextlib import contextmanager
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
from pydantic import BaseModel, Field
from enum import Enum
from camera_service.models import IdentitySeen
from camera_service.object_security.alerts import ObjectSecurityAlerter
from camera_service.domain import ResourceScope
from camera_service.inference import UltralyticsCPUBackend

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
    tracking_mode: str = "detect"
    features: CameraFeatures = Field(default_factory=CameraFeatures)
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

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

class CameraManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        self._tracking_models = {}
        self._inference_backends = {}
        self._tracking_error = None
        self._stream_active_tracks = {}
        self._alert_last_sent = {}
        self._track_identity_cache = {}
        self._full_frame_face_cache = {}
        self._tracking_stream_state = {}
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
                FOREIGN KEY(camera_id) REFERENCES cameras(camera_id)
            );
            ''')
            self._ensure_column(c, 'cameras', 'camera_zone', "TEXT NOT NULL DEFAULT 'inside'")
            self._ensure_column(c, 'cameras', 'crowd_threshold', 'INTEGER NOT NULL DEFAULT 10')
            self._ensure_column(c, 'cameras', 'tracking_fps', 'REAL NOT NULL DEFAULT 3.0')
            self._ensure_column(c, 'cameras', 'tracking_imgsz', 'INTEGER NOT NULL DEFAULT 384')
            self._ensure_column(c, 'cameras', 'tracking_quality', 'INTEGER NOT NULL DEFAULT 65')
            self._ensure_column(c, 'cameras', 'tracking_mode', "TEXT NOT NULL DEFAULT 'detect'")

    def _ensure_column(self, conn, table: str, column: str, definition: str):
        existing = {row['name'] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _mask_rtsp_password(self, rtsp_url: str) -> str:
        """Mask password in RTSP URL for security"""
        if not rtsp_url:
            return rtsp_url
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
            tracking_fps=max(1.0, min(float(camera_data.get('tracking_fps', 3.0) or 3.0), 12.0)),
            tracking_imgsz=max(256, min(int(camera_data.get('tracking_imgsz', 384) or 384), 640)),
            tracking_quality=max(35, min(int(camera_data.get('tracking_quality', 65) or 65), 95)),
            tracking_mode=str(camera_data.get('tracking_mode', 'detect') or 'detect').strip().lower() if str(camera_data.get('tracking_mode', 'detect') or 'detect').strip().lower() in {'detect','track'} else 'detect',
            features=CameraFeatures(**features),
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat()
        )

        with self._lock, self._conn() as c:
            c.execute('''
                INSERT INTO cameras
                (id, camera_id, name, source_type, rtsp_url, enabled, camera_role,
                 camera_zone, crowd_threshold, tracking_fps, tracking_imgsz,
                 tracking_quality, tracking_mode, features_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                json.dumps(config.features.model_dump()),
                config.created_at,
                config.updated_at
            ))
            c.execute("INSERT OR REPLACE INTO camera_status(camera_id,state,online) VALUES(?,?,0)",(config.camera_id,CameraState.STOPPED.value))

        return config

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
                    features=CameraFeatures(**json.loads(row['features_json'])),
                    created_at=row['created_at'],
                    updated_at=row['updated_at']
                ) for row in rows
            ]

    def update_camera(self, camera_id: str, updates: Dict[str, Any]) -> Optional[CameraConfig]:
        """Update camera configuration"""
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
            if 'features' in updates:
                camera.features = CameraFeatures(**updates['features'])

            camera.updated_at = datetime.now(timezone.utc).isoformat()

            with self._conn() as c:
                c.execute('''
                    UPDATE cameras
                    SET name = ?, source_type = ?, rtsp_url = ?, enabled = ?,
                        camera_role = ?, camera_zone = ?, crowd_threshold = ?,
                        tracking_fps = ?, tracking_imgsz = ?, tracking_quality = ?,
                        tracking_mode = ?,
                        features_json = ?, updated_at = ?
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
                    json.dumps(camera.features.model_dump()),
                    camera.updated_at,
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
            root = Path("data/evidence") / camera_id
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"{prefix}_{int(time.time() * 1000)}.jpg"
            cv2.imwrite(str(path), frame)
            return str(path)
        except Exception:
            return None

    def _begin_security_clip(self, stream_state: dict | None, alert_id: str, camera_id: str):
        if stream_state is None or stream_state.get("security_clip"):
            return
        stream_state["security_clip"] = {
            "alert_id": alert_id,
            "camera_id": camera_id,
            "started_at": time.monotonic(),
            "duration": 10.0,
            "frames": [],
        }

    def _record_security_clip_frame(self, stream_state: dict | None, frame, store=None):
        if stream_state is None or not stream_state.get("security_clip"):
            return
        clip = stream_state["security_clip"]
        clip["frames"].append(frame.copy())
        if time.monotonic() - clip["started_at"] >= clip["duration"]:
            self._finalize_security_clip(stream_state, store)

    def _finalize_security_clip(self, stream_state: dict | None, store=None):
        if stream_state is None or not stream_state.get("security_clip"):
            return
        clip = stream_state.pop("security_clip")
        frames = clip.get("frames") or []
        if not frames:
            return
        thread = threading.Thread(target=self._write_security_clip, args=(clip, frames, store), daemon=True)
        thread.start()

    def _write_security_clip(self, clip: dict, frames: list, store=None):
        try:
            root = Path("data/evidence") / clip["camera_id"]
            root.mkdir(parents=True, exist_ok=True)
            path = root / f"security_clip_{int(time.time() * 1000)}.mp4"
            h, w = frames[0].shape[:2]
            writer = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), max(1.0, len(frames) / max(1.0, clip["duration"])), (w, h))
            for frame in frames:
                if frame.shape[:2] != (h, w):
                    frame = cv2.resize(frame, (w, h))
                writer.write(frame)
            writer.release()
            if store is not None:
                store.update_security_alert_clip(clip["alert_id"], str(path))
        except Exception:
            return

    def _prepare_tracking_frame(self, frame, camera_config=None):
        """Downscale large camera frames before AI/streaming for CPU-friendly demos."""
        max_width = max(480, min(int(getattr(camera_config, "tracking_imgsz", 384) or 384) * 2, 960))
        h, w = frame.shape[:2]
        if w <= max_width:
            return frame
        scale = max_width / float(w)
        return cv2.resize(frame, (max_width, max(1, int(h * scale))), interpolation=cv2.INTER_AREA)

    def _should_run_tracking_ai(self, stream_state: dict | None, camera_config=None) -> bool:
        if stream_state is None:
            return True
        now = time.monotonic()
        ai_fps = max(0.5, min(float(getattr(camera_config, "tracking_fps", 2.0) or 2.0), 8.0))
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
            imgsz = int(getattr(camera_config, "tracking_imgsz", 384) or 384)
            mode = getattr(camera_config, "tracking_mode", "detect") or "detect"
            use_router = os.environ.get("SNAPKEY_INFERENCE_ROUTER_ENABLED", "").strip().lower() in {"1", "true", "yes", "on"}

            if use_router:
                backend = self._inference_backends.get(model_path)
                if backend is None:
                    backend = UltralyticsCPUBackend(model_path)
                    self._inference_backends[model_path] = backend
                camera_id_for_scope = getattr(camera_config, "camera_id", None) or "camera-unknown"
                scope = ResourceScope(
                    tenant_id=os.environ.get("SNAPKEY_TENANT_ID", "legacy-tenant"),
                    company_code=os.environ.get("SNAPKEY_COMPANY_CODE") or None,
                    shop_id=os.environ.get("SNAPKEY_SHOP_ID", "legacy-shop"),
                    edge_id=os.environ.get("SNAPKEY_EDGE_ID", "legacy-edge"),
                    camera_id=camera_id_for_scope,
                )
                normalized = backend.infer(
                    frame,
                    scope=scope,
                    frame_id=f"{camera_id_for_scope}-{time.time_ns()}",
                    tracking=(mode == "track"),
                    imgsz=imgsz,
                    conf=0.20,
                    max_det=40,
                )
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
                if mode != "track":
                    track_ids = list(range(1, len(xyxy) + 1))
                elif any(track_id is None for track_id in track_ids):
                    track_ids = [
                        track_id if track_id is not None else index
                        for index, track_id in enumerate(track_ids, start=1)
                    ]
                if stream_state is not None:
                    stream_state["inference_backend"] = normalized.backend_type.value
                    stream_state["inference_latency_ms"] = normalized.inference_latency_ms
            else:
                model = self._tracking_models.get(model_path)
                if model is None:
                    from ultralytics import YOLO

                    model = YOLO(model_path)
                    self._tracking_models[model_path] = model

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
                track_ids = boxes.id.int().cpu().tolist() if boxes.id is not None else list(range(1, len(xyxy) + 1))
            active_known_tracks = set()
            now = time.monotonic()
            camera_id = camera_config.camera_id if camera_config is not None else None
            camera_zone = camera_config.camera_zone.value if camera_config is not None else "inside"
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
                        faces = face_service.detect(roi)
                        if faces:
                            best = max(faces, key=lambda face: face_service.quality(face, roi.shape))
                            if face_service.quality(best, roi.shape) >= recognition_config.minimum_face_quality:
                                match, score = face_service.recognize(best.get("embedding"), recognition_config.known_threshold)
                                if match:
                                    active_known_tracks.add(str(track_id))
                                    snapshot_path = self._save_event_snapshot(roi, camera_id, "known")
                                    recognized_name = match["full_name"]
                                    recognized_text = f"{recognized_name} {score:.2f}"
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
                                elif camera_zone == "inside" and store and self._should_emit_alert(f"unknown:{camera_id}:{track_id}", 20):
                                    snapshot_path = self._save_event_snapshot(roi, camera_id, "unknown")
                                    store.add_person_event(None, getattr(attendance_engine, "store_id", "store-1"), camera_id, "UNKNOWN_INSIDE_ALERT", datetime.now(timezone.utc), {"track_id": track_id, "score": score, "snapshot_path": snapshot_path})
                                    self._track_identity_cache[cache_key] = {
                                        "checked_at": now,
                                        "person_id": None,
                                        "text": f"Unknown person {score:.2f}",
                                        "score": score,
                                    }
                            else:
                                self._track_identity_cache[cache_key] = {
                                    "checked_at": now,
                                    "person_id": None,
                                    "text": "Face too small/blurred",
                                    "score": 0.0,
                                }
                        else:
                            self._track_identity_cache[cache_key] = {
                                "checked_at": now,
                                "person_id": None,
                                "text": None,
                                "score": 0.0,
                            }

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
                    security_key = f"object-security:{object_security_model_path}"
                    security_model = self._tracking_models.get(security_key)
                    if security_model is None:
                        from ultralytics import YOLO

                        security_model = YOLO(object_security_model_path)
                        self._tracking_models[security_key] = security_model
                    security_imgsz = max(416, min(int(getattr(camera_config, "tracking_imgsz", 512) or 512), 640))
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
                f"Tracking unavailable: {exc}",
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
        quality = max(35, min(int(getattr(camera_config, "tracking_quality", 65) or 65), 95))
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
            'source': source_text,
            'attempts': [],
            'resolution': None,
            'fps': 0,
            'connection_ms': 0,
            'frames_received': 0
        }

        start_time = time.time()

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
            result['message'] = f'Connection error: {str(e)}'

        return result

    def get_camera_status(self, camera_id: str) -> Optional[CameraStatus]:
        """Get current camera status"""
        with self._conn() as c:
            row = c.execute('''
                SELECT * FROM camera_status WHERE camera_id = ?
            ''', (camera_id,)).fetchone()

            if not row:
                return None

            return CameraStatus(
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
                last_error=row['last_error']
            )

    def update_camera_status(self, camera_id: str, status: CameraStatus):
        """Update camera status"""
        with self._lock, self._conn() as c:
            c.execute('''
                INSERT OR REPLACE INTO camera_status
                (camera_id, state, online, last_frame_at, capture_fps, ai_fps,
                 frames_received, frames_dropped, reconnect_count, last_error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                status.last_error
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

    def iter_tracking_mjpeg(self, camera_config: CameraConfig, model_path: str, face_service=None, recognition_config=None, attendance_engine=None, store=None, object_security_model_path: str | None = None, object_security_confidence: float = 0.55):
        """Yield MJPEG frames from a latest-frame pipeline.

        Capture, AI, and browser streaming run independently so slow CPU
        inference cannot freeze the client-facing live view.
        """
        rtsp_url = camera_config.rtsp_url
        target_ai_fps = max(0.5, min(float(getattr(camera_config, "tracking_fps", 2.0) or 2.0), 8.0))
        display_fps = max(6.0, min(target_ai_fps * 4.0, 12.0))
        frame_delay = 1.0 / display_fps
        stream_state = {
            "security_clip": None,
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
        stop_event = threading.Event()
        frame_lock = threading.Lock()
        result_lock = threading.Lock()
        capture_state = {
            "latest_frame": None,
            "latest_seq": 0,
            "ai_seq": 0,
            "stream_seq": 0,
            "frames_received": 0,
            "frames_dropped": 0,
            "stopped": False,
            "error": None,
        }
        result_state = {
            "annotated_frame": None,
            "updated_at": 0.0,
            "error": None,
        }

        def publish_frame(frame):
            prepared = self._prepare_tracking_frame(frame, camera_config)
            with frame_lock:
                if capture_state["latest_seq"] > capture_state["ai_seq"]:
                    capture_state["frames_dropped"] += 1
                capture_state["latest_frame"] = prepared
                capture_state["latest_seq"] += 1
                capture_state["frames_received"] += 1

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
                finally:
                    cap.release()
            except Exception as exc:
                capture_state["error"] = str(exc)
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
                        result_state["annotated_frame"] = ai_frame.copy()
                        result_state["updated_at"] = time.monotonic()
                        result_state["error"] = None
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

                    if stream_state.get("latest_overlays"):
                        annotated = self._draw_tracking_demo_overlay(frame, camera_config, stream_state, attendance_engine, store)
                    elif cached_annotated is not None and cached_age <= 3.0:
                        annotated = cached_annotated.copy()
                    else:
                        annotated = self._draw_tracking_demo_overlay(frame, camera_config, stream_state, attendance_engine, store)
                        if ai_error:
                            cv2.putText(
                                annotated,
                                f"AI: {ai_error[:44]}",
                                (20, max(56, min(annotated.shape[0] - 20, 62))),
                                cv2.FONT_HERSHEY_SIMPLEX,
                                0.55,
                                (0, 0, 255),
                                2,
                            )
                    self._record_security_clip_frame(stream_state, frame, store)
                    quality = max(35, min(int(getattr(camera_config, "tracking_quality", 65) or 65), 95))
                    ok, encoded = cv2.imencode(".jpg", annotated, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
                    if ok and encoded is not None:
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
            self._finalize_security_clip(stream_state, store)
            current_thread = threading.current_thread()
            if capture_thread is not current_thread:
                capture_thread.join(timeout=1.0)
            if ai_thread is not current_thread:
                ai_thread.join(timeout=1.0)
