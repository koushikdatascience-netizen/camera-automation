from __future__ import annotations

from camera_service.domain.inference import BackendType
from .gpu import UltralyticsGPUBackend, detect_local_gpu
from .router import InferenceBackendRouter
from .ultralytics_adapter import UltralyticsCPUBackend


def build_local_backends(model_path: str):
    """CPU is mandatory; GPU is registered only when the machine supports it."""
    router = InferenceBackendRouter()
    cpu = UltralyticsCPUBackend(model_path)
    router.register(cpu)
    capability = detect_local_gpu()
    if capability["available"] and capability["provider"] == "cuda":
        router.register(UltralyticsGPUBackend(model_path, device=str(capability["device"])))
    return router, capability


def choose_local_backend(router: InferenceBackendRouter, prefer_gpu: bool = True):
    if prefer_gpu and BackendType.LOCAL_GPU in router.available():
        return router.get(BackendType.LOCAL_GPU)
    return router.get(BackendType.LOCAL_CPU)
