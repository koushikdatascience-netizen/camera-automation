from __future__ import annotations

from camera_service.domain.inference import BackendType

from .base import InferenceBackend


class InferenceBackendRouter:
    """Small registry used to select an explicitly configured inference backend."""

    def __init__(self) -> None:
        self._backends: dict[BackendType, InferenceBackend] = {}

    def register(self, backend: InferenceBackend) -> None:
        self._backends[backend.backend_type] = backend

    def get(self, backend_type: BackendType) -> InferenceBackend:
        try:
            return self._backends[backend_type]
        except KeyError as exc:
            raise RuntimeError(f"Inference backend {backend_type.value} is not registered") from exc

    def available(self) -> list[BackendType]:
        return list(self._backends)
