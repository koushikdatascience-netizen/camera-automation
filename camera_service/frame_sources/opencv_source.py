from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import cv2

from .base import FramePacket, FrameSource


class OpenCVFrameSource(FrameSource):
    """OpenCV capture source usable with MP4 files, webcams, and RTSP URLs."""

    def __init__(self, source: str | int, *, backend: int | None = None) -> None:
        self.source = source
        self.backend = backend
        self._capture: Any = None
        self._frame_number = 0

    def open(self) -> None:
        if isinstance(self.source, str) and "://" not in self.source:
            path = Path(self.source)
            if not path.exists():
                raise FileNotFoundError(f"Video source does not exist: {path}")
        self._capture = (
            cv2.VideoCapture(self.source)
            if self.backend is None
            else cv2.VideoCapture(self.source, self.backend)
        )
        if not self._capture.isOpened():
            self.close()
            raise RuntimeError(f"Could not open video source: {self.source}")

    def read(self) -> FramePacket | None:
        if self._capture is None:
            raise RuntimeError("Frame source is not open")
        ok, frame = self._capture.read()
        if not ok or frame is None:
            return None
        self._frame_number += 1
        position_ms = float(self._capture.get(cv2.CAP_PROP_POS_MSEC))
        return FramePacket(
            frame_id=f"frame-{self._frame_number:08d}",
            captured_at=datetime.now(timezone.utc),
            frame=frame,
            source_position_ms=position_ms if position_ms >= 0 else None,
        )

    def close(self) -> None:
        if self._capture is not None:
            self._capture.release()
            self._capture = None
