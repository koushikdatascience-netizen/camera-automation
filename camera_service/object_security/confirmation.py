from __future__ import annotations

from collections import deque


class TemporalConfirmation:
    def __init__(self, window_frames: int = 5, required_hits: int = 3, minimum_confidence: float = 0.30):
        self.window_frames = max(1, int(window_frames))
        self.required_hits = max(1, int(required_hits))
        self.minimum_confidence = float(minimum_confidence)
        self._tracks: dict[str, deque[bool]] = {}

    def update(self, track_id: str, confidence: float) -> str:
        hits = self._tracks.setdefault(str(track_id), deque(maxlen=self.window_frames))
        hits.append(float(confidence) >= self.minimum_confidence)
        total = sum(1 for hit in hits if hit)
        if total >= self.required_hits:
            return "CONFIRMED"
        if total > 0:
            return "VERIFYING"
        return "DETECTED"

    def forget(self, active_track_ids: set[str]) -> None:
        for track_id in list(self._tracks):
            if track_id not in active_track_ids:
                self._tracks.pop(track_id, None)
