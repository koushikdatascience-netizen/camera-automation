"""Inference backend abstraction.

Existing inference remains untouched until it is adapted behind these contracts.
"""

from .base import InferenceBackend
from .router import InferenceBackendRouter
from .ultralytics_adapter import UltralyticsCPUBackend, normalize_ultralytics_results

__all__ = [
    "InferenceBackend",
    "InferenceBackendRouter",
    "UltralyticsCPUBackend",
    "normalize_ultralytics_results",
]
