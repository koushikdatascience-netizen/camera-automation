from __future__ import annotations
from contextlib import contextmanager
import json, sqlite3, threading, uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
import re
import cv2
import time
import sys
from pydantic import BaseModel, Field
from enum import Enum

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

class CameraFeatures(BaseModel):
    attendance: bool = False
    face_recognition: bool = False
    unknown_detection: bool = False
    shoplifting: bool = False

class CameraConfig(BaseModel):
    camera_id: str
    name: str
    source_type: str = "rtsp"
    rtsp_url: str
    enabled: bool = True
    camera_role: CameraRole = CameraRole.GENERAL
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
            features=CameraFeatures(**features),
            created_at=datetime.now(timezone.utc).isoformat(),
            updated_at=datetime.now(timezone.utc).isoformat()
        )

        with self._lock, self._conn() as c:
            c.execute('''
                INSERT INTO cameras VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                str(uuid.uuid4()),
                config.camera_id,
                config.name,
                config.source_type,
                config.rtsp_url,
                1 if config.enabled else 0,
                config.camera_role.value,
                json.dumps(config.features.model_dump()),
                config.created_at,
                config.updated_at
            ))

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
                camera.camera_role = updates['camera_role']
            if 'features' in updates:
                camera.features = CameraFeatures(**updates['features'])

            camera.updated_at = datetime.now(timezone.utc).isoformat()

            with self._conn() as c:
                c.execute('''
                    UPDATE cameras
                    SET name = ?, source_type = ?, rtsp_url = ?, enabled = ?,
                        camera_role = ?, features_json = ?, updated_at = ?
                    WHERE camera_id = ?
                ''', (
                    camera.name,
                    camera.source_type,
                    camera.rtsp_url,
                    1 if camera.enabled else 0,
                    camera.camera_role.value,
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
        return cap

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
