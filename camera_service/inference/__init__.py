"""Inference backend abstraction.

Existing inference remains untouched until it is adapted behind these contracts.
"""

from .base import InferenceBackend
from .router import InferenceBackendRouter

__all__ = ["InferenceBackend", "InferenceBackendRouter"]
