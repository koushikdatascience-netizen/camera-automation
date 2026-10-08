"""Backend-neutral Vision inference contracts."""

from .base import InferenceBackend
from .router import InferenceBackendRouter
from .ultralytics_adapter import UltralyticsCPUBackend, normalize_ultralytics_results
from .gpu import UltralyticsGPUBackend, detect_local_gpu
from .cloud import CloudGPUBackend
from .factory import build_local_backends, choose_local_backend
from .runtime import build_runtime_router, select_runtime_backend

__all__ = [
    "InferenceBackend", "InferenceBackendRouter",
    "UltralyticsCPUBackend", "UltralyticsGPUBackend", "CloudGPUBackend",
    "normalize_ultralytics_results", "detect_local_gpu",
    "build_local_backends", "choose_local_backend",
    "build_runtime_router", "select_runtime_backend",
]
