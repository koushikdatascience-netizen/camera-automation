from __future__ import annotations

from camera_service.domain.events import EdgeEvent


class EventPublisher:
    """Single event boundary. Storage adapters can keep existing SQLite queue semantics."""

    def __init__(self, enqueue):
        self._enqueue = enqueue

    def publish(self, event: EdgeEvent) -> str:
        payload = event.model_dump(mode="json")
        self._enqueue(payload)
        return event.event_id
