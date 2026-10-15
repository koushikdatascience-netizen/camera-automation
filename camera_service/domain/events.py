from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

from .identifiers import ResourceScope

SCHEMA_VERSION = 1


class EventType(str, Enum):
    ATTENDANCE_ENTRY = "ATTENDANCE_ENTRY"
    ATTENDANCE_EXIT = "ATTENDANCE_EXIT"
    BREAK = "BREAK"
    UNKNOWN_PERSON = "UNKNOWN_PERSON"
    SCISSORS_ALERT = "SCISSORS_ALERT"
    JEWELLERY_TAG_THREAT = "JEWELLERY_TAG_THREAT"
    COMPOUND_SCISSORS_TAG_THREAT = "COMPOUND_SCISSORS_TAG_THREAT"
    CRITICAL_ALARM = "CRITICAL_ALARM"
    CAMERA_OFFLINE = "CAMERA_OFFLINE"
    CAMERA_ONLINE = "CAMERA_ONLINE"
    EDGE_OFFLINE = "EDGE_OFFLINE"
    EDGE_ONLINE = "EDGE_ONLINE"
    MODEL_FAILURE = "MODEL_FAILURE"
    SYSTEM_DEGRADATION = "SYSTEM_DEGRADATION"


class EdgeEvent(BaseModel):
    schema_version: int = SCHEMA_VERSION
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: EventType
    scope: ResourceScope
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)
