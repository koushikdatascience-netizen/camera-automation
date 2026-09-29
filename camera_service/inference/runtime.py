from __future__ import annotations

import os
from typing import Any

from camera_service.domain.inference import BackendType

from .cloud import CloudGPUBackend
from .gpu import UltralyticsGPUBackend, detect_local_gpu
from .router import InferenceBackendRouter
from .ultralytics_adapter import UltralyticsCPUBackend


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def build_runtime_router(model_path: str) -> tuple[InferenceBackendRouter, dict[str, Any]]:
    """Build only the inference backends this edge can actually use."""
    router = InferenceBackendRouter()
    router.register(UltralyticsCPUBackend(model_path))

    local_gpu = detect_local_gpu()
    if local_gpu["available"] and local_gpu["provider"] == "cuda":
        router.register(UltralyticsGPUBackend(model_path, device=str(local_gpu["device"])))

    cloud_endpoint = os.environ.get("SNAPKEY_CLOUD_INFERENCE_URL", "").strip()
    cloud_token = os.environ.get("SNAPKEY_CLOUD_INFERENCE_TOKEN", "").strip()
    cloud_configured = bool(cloud_endpoint and cloud_token)
    if cloud_configured:
        timeout = float(os.environ.get("SNAPKEY_CLOUD_INFERENCE_TIMEOUT_SECONDS", "10") or 10)
        router.register(CloudGPUBackend(cloud_endpoint, cloud_token, timeout_seconds=max(1.0, timeout)))

    capability = {
        "local_gpu": local_gpu,
        "cloud_gpu_configured": cloud_configured,
        "available_backends": [backend.value for backend in router.available()],
    }
    return router, capability


def select_runtime_backend(router: InferenceBackendRouter):
    """Select backend from explicit mode or AUTO.

    AUTO is edge-first: usable local GPU -> local CPU. Cloud GPU is selected
    only when explicitly requested, so an internet outage never disables the
    customer's baseline local AI.
    """
    raw_mode = os.environ.get("SNAPKEY_INFERENCE_BACKEND", "AUTO").strip().upper() or "AUTO"
    if raw_mode == "AUTO":
        if BackendType.LOCAL_GPU in router.available():
            return router.get(BackendType.LOCAL_GPU)
        return router.get(BackendType.LOCAL_CPU)

    try:
        requested = BackendType(raw_mode)
    except ValueError as exc:
        raise RuntimeError(
            "SNAPKEY_INFERENCE_BACKEND must be AUTO, LOCAL_CPU, LOCAL_GPU, or CLOUD_GPU"
        ) from exc

    if requested not in router.available():
        if _truthy(os.environ.get("SNAPKEY_INFERENCE_ALLOW_FALLBACK", "1")):
            if BackendType.LOCAL_GPU in router.available():
                return router.get(BackendType.LOCAL_GPU)
            return router.get(BackendType.LOCAL_CPU)
        raise RuntimeError(f"Requested inference backend {requested.value} is unavailable")
    return router.get(requested)
