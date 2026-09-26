from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from camera_service.domain.inference import BackendType, InferenceResult
from camera_service.domain.identifiers import ResourceScope


class InferenceBackend(ABC):
    """Backend-neutral inference contract.

    Implementations must return the same normalized result regardless of whether
    inference executes on local CPU, local GPU, or a remote GPU worker.
    """

    backend_type: BackendType

    @abstractmethod
    def infer(
        self,
        frame: Any,
        *,
        scope: ResourceScope,
        frame_id: str,
        model_id: str,
        model_version: str,
    ) -> InferenceResult:
        raise NotImplementedError

    def health(self) -> dict[str, Any]:
        return {"backend": self.backend_type.value, "ready": True}
