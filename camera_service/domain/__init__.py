"""Shared Vision domain contracts for edge/cloud interoperability."""

from .identifiers import ResourceScope
from .inference import BackendType, Detection, InferenceResult
from .events import EdgeEvent, EventType, SCHEMA_VERSION

__all__ = [
    "ResourceScope",
    "BackendType",
    "Detection",
    "InferenceResult",
    "EdgeEvent",
    "EventType",
    "SCHEMA_VERSION",
]
