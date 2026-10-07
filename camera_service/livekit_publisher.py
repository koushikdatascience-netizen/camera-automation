from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

import cv2


logger = logging.getLogger(__name__)


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
        # Live Cameras supports every configured camera. Attendance cameras use
        # the same annotated edge frame, so names/unknown boxes match the local EXE.
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
            if self._sessions.get(session_id) is session:
                self._sessions.pop(session_id, None)
        return {"stopped": True, "session_id": session_id, "camera_id": session.camera_id}

    def _thread_main(self, session_id: str, camera_id: str, url: str, token: str, ttl: int, stop_event: threading.Event) -> None:
        try:
            logger.info("[LIVE_VIEW] publisher started %s", json.dumps({"session_id":session_id,"camera_id":camera_id}))
            asyncio.run(self._publish(camera_id, url, token, ttl, stop_event))
        except Exception as exc:
            logger.error("[LIVE_VIEW] publisher error %s", json.dumps({"session_id":session_id,
                "camera_id":camera_id,"error_type":type(exc).__name__}))
        finally:
            logger.info("[LIVE_VIEW] publisher stopped %s", json.dumps({"session_id":session_id,"camera_id":camera_id}))
            with self._lock:
                current=self._sessions.get(session_id)
                if current is not None and current.stop_event is stop_event:
                    self._sessions.pop(session_id, None)

    async def _publish(self, camera_id: str, url: str, token: str, ttl: int, stop_event: threading.Event) -> None:
        try:
            from livekit import rtc
        except ImportError as exc:
            raise RuntimeError("LiveKit RTC dependency is not installed") from exc

        # Do not open RTSP/webcam here. The normal Camera Eye worker owns capture and
        # inference. We publish its latest annotated frame so remote viewers see the
        # same bounding boxes, recognized names and AI status without duplicate load.
        deadline = time.monotonic() + ttl
        first_frame = None
        while not stop_event.is_set() and time.monotonic() < deadline:
            first_frame = self.camera_manager.get_live_ai_frame(camera_id)
            if first_frame is not None:
                break
            await asyncio.sleep(0.1)
        if first_frame is None:
            raise RuntimeError("No fresh annotated AI frame is available; ensure the attendance camera worker is online")

        room = rtc.Room()
        height, width = first_frame.shape[:2]
        source = rtc.VideoSource(width, height)
        try:
            logger.info("[LIVE_VIEW] edge connecting %s", json.dumps({"camera_id":camera_id}))
            await room.connect(url, token, rtc.RoomOptions(auto_subscribe=False, dynacast=True))
            logger.info("[LIVE_VIEW] edge connected %s", json.dumps({"camera_id":camera_id,"room":room.name}))
            track = rtc.LocalVideoTrack.create_video_track("attendance-ai-camera", source)
            publication = await room.local_participant.publish_track(
                track,
                rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA),
            )
            logger.info("[LIVE_VIEW] edge track published %s", json.dumps({"camera_id":camera_id,
                "room":room.name,"track_sid":publication.sid,"source":"CAMERA"}))
            frame_interval = 1.0 / 10.0
            last_frame = first_frame
            while not stop_event.is_set() and time.monotonic() < deadline:
                started = time.monotonic()
                frame = self.camera_manager.get_live_ai_frame(camera_id)
                if frame is not None:
                    last_frame = frame
                frame = last_frame
                h, w = frame.shape[:2]
                if w != width or h != height:
                    frame = cv2.resize(frame, (width, height))
                rgba = cv2.cvtColor(frame, cv2.COLOR_BGR2RGBA)
                source.capture_frame(rtc.VideoFrame(width, height, rtc.VideoBufferType.RGBA, rgba.tobytes()))
                await asyncio.sleep(max(0.0, frame_interval - (time.monotonic() - started)))
        finally:
            if room.isconnected():
                await room.disconnect()
