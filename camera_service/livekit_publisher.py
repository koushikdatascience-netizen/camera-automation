from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass
from typing import Any

import cv2


@dataclass
class LiveViewSession:
    session_id: str
    camera_id: str
    stop_event: threading.Event
    thread: threading.Thread
    expires_at_monotonic: float


class LiveKitCameraPublisher:
    """Temporary, on-demand WebRTC publisher for one edge camera.

    The edge connects directly to LiveKit. Camera credentials and raw frames never
    pass through the Camera Eye FastAPI/cloud portal process.
    """

    def __init__(self, camera_manager):
        self.camera_manager = camera_manager
        self._lock = threading.RLock()
        self._sessions: dict[str, LiveViewSession] = {}

    def start(self, *, session_id: str, camera_id: str, url: str, token: str, ttl_seconds: int = 600) -> dict[str, Any]:
        camera = self.camera_manager.get_camera(camera_id)
        if not camera:
            raise RuntimeError(f"Camera {camera_id} was not found on this edge")
        if str(getattr(camera.camera_role, "value", camera.camera_role)).upper() != "ENTRANCE_EXIT":
            raise RuntimeError("Remote live view is restricted to attendance cameras")
        ttl = max(30, min(1800, int(ttl_seconds)))
        self.stop(session_id)
        stop_event = threading.Event()
        thread = threading.Thread(
            target=self._thread_main,
            args=(session_id, camera_id, url, token, ttl, stop_event),
            name=f"livekit-camera-{camera_id}",
            daemon=True,
        )
        session = LiveViewSession(session_id, camera_id, stop_event, thread, time.monotonic() + ttl)
        with self._lock:
            self._sessions[session_id] = session
        thread.start()
        return {"started": True, "session_id": session_id, "camera_id": camera_id, "ttl_seconds": ttl}

    def stop(self, session_id: str) -> dict[str, Any]:
        with self._lock:
            session = self._sessions.get(session_id)
        if not session:
            return {"stopped": True, "session_id": session_id, "already_stopped": True}
        session.stop_event.set()
        if session.thread.is_alive() and session.thread is not threading.current_thread():
            session.thread.join(timeout=3)
        with self._lock:
            self._sessions.pop(session_id, None)
        return {"stopped": True, "session_id": session_id, "camera_id": session.camera_id}

    def _thread_main(self, session_id: str, camera_id: str, url: str, token: str, ttl: int, stop_event: threading.Event) -> None:
        try:
            asyncio.run(self._publish(camera_id, url, token, ttl, stop_event))
        finally:
            with self._lock:
                self._sessions.pop(session_id, None)

    async def _publish(self, camera_id: str, url: str, token: str, ttl: int, stop_event: threading.Event) -> None:
        try:
            from livekit import rtc
        except ImportError as exc:
            raise RuntimeError("LiveKit RTC dependency is not installed") from exc

        camera = self.camera_manager.get_camera(camera_id)
        if not camera:
            raise RuntimeError(f"Camera {camera_id} disappeared before live view started")
        raw = str(camera.rtsp_url)
        source_value: int | str = int(raw) if camera.source_type in {"webcam", "dshow"} and raw.isdigit() else raw
        cap = cv2.VideoCapture(source_value)
        if not cap.isOpened():
            cap.release()
            raise RuntimeError("Unable to open attendance camera for remote live view")

        room = rtc.Room()
        deadline = time.monotonic() + ttl
        try:
            await room.connect(url, token, rtc.RoomOptions(auto_subscribe=False, dynacast=True))
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError("Attendance camera opened but did not return a frame")
            height, width = frame.shape[:2]
            source = rtc.VideoSource(width, height)
            track = rtc.LocalVideoTrack.create_video_track("attendance-camera", source)
            await room.local_participant.publish_track(
                track,
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA),
            )
            frame_interval = 1.0 / 10.0
            while not stop_event.is_set() and time.monotonic() < deadline:
                started = time.monotonic()
                if frame is None:
                    ok, frame = cap.read()
                if not ok or frame is None:
                    await asyncio.sleep(0.15)
                    frame = None
                    continue
                rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
                h, w = rgba.shape[:2]
                if w != width or h != height:
                    rgba = cv2.resize(rgba, (width, height))
                source.capture_frame(rtc.VideoFrame(width, height, rtc.VideoBufferType.RGBA, rgba.tobytes()))
                ok, frame = cap.read()
                await asyncio.sleep(max(0.0, frame_interval - (time.monotonic() - started)))
        finally:
            cap.release()
            if room.isconnected():
                await room.disconnect()
