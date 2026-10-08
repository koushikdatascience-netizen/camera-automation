from types import SimpleNamespace

from camera_service.domain.inference import BackendType
from camera_service.inference.router import InferenceBackendRouter
from camera_service.inference.runtime import select_runtime_backend


class Backend:
    def __init__(self, backend_type):
        self.backend_type = backend_type


def router_with(*types):
    router = InferenceBackendRouter()
    for backend_type in types:
        router.register(Backend(backend_type))
    return router


def test_auto_prefers_local_gpu(monkeypatch):
    monkeypatch.setenv("SNAPKEY_INFERENCE_BACKEND", "AUTO")
    router = router_with(BackendType.LOCAL_CPU, BackendType.LOCAL_GPU, BackendType.CLOUD_GPU)
    assert select_runtime_backend(router).backend_type == BackendType.LOCAL_GPU


def test_auto_keeps_cloud_as_optional_enhancement(monkeypatch):
    monkeypatch.setenv("SNAPKEY_INFERENCE_BACKEND", "AUTO")
    router = router_with(BackendType.LOCAL_CPU, BackendType.CLOUD_GPU)
    assert select_runtime_backend(router).backend_type == BackendType.LOCAL_CPU


def test_explicit_cloud_gpu(monkeypatch):
    monkeypatch.setenv("SNAPKEY_INFERENCE_BACKEND", "CLOUD_GPU")
    router = router_with(BackendType.LOCAL_CPU, BackendType.CLOUD_GPU)
    assert select_runtime_backend(router).backend_type == BackendType.CLOUD_GPU


def test_unavailable_explicit_backend_falls_back_locally(monkeypatch):
    monkeypatch.setenv("SNAPKEY_INFERENCE_BACKEND", "LOCAL_GPU")
    monkeypatch.setenv("SNAPKEY_INFERENCE_ALLOW_FALLBACK", "1")
    router = router_with(BackendType.LOCAL_CPU)
    assert select_runtime_backend(router).backend_type == BackendType.LOCAL_CPU


def test_unavailable_explicit_backend_can_fail_closed(monkeypatch):
    monkeypatch.setenv("SNAPKEY_INFERENCE_BACKEND", "CLOUD_GPU")
    monkeypatch.setenv("SNAPKEY_INFERENCE_ALLOW_FALLBACK", "0")
    router = router_with(BackendType.LOCAL_CPU)
    try:
        select_runtime_backend(router)
    except RuntimeError as exc:
        assert "unavailable" in str(exc)
    else:
        raise AssertionError("expected unavailable backend error")
