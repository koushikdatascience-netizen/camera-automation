from camera_service.domain import BackendType
from camera_service.inference.factory import choose_local_backend
from camera_service.inference.router import InferenceBackendRouter


class Dummy:
    def __init__(self, backend_type): self.backend_type = backend_type


def test_cpu_fallback_is_mandatory():
    router = InferenceBackendRouter()
    cpu = Dummy(BackendType.LOCAL_CPU)
    router.register(cpu)
    assert choose_local_backend(router, prefer_gpu=True) is cpu


def test_gpu_is_selected_only_when_registered():
    router = InferenceBackendRouter()
    cpu, gpu = Dummy(BackendType.LOCAL_CPU), Dummy(BackendType.LOCAL_GPU)
    router.register(cpu); router.register(gpu)
    assert choose_local_backend(router, prefer_gpu=True) is gpu
    assert choose_local_backend(router, prefer_gpu=False) is cpu
