from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from camera_service.model_manager import EdgeModelManager


class FakeCloud:
    def __init__(self, payload: bytes, sha: str | None = None):
        self.payload = payload
        self.sha = sha or hashlib.sha256(payload).hexdigest()
        self.downloads = 0
    def enabled(self): return True
    def model_manifest(self):
        return {"models": [{"id":"person-detection","version":"1.0.0","runtime":"ONNX","required":True,
                            "sha256":self.sha,"filename":"yolo26n.onnx","size_bytes":len(self.payload)}]}
    def download_model(self, model_id, version, destination: Path):
        self.downloads += 1
        destination.write_bytes(self.payload)


def test_model_manager_downloads_verifies_and_reuses_cache(tmp_path):
    cloud=FakeCloud(b"verified-model")
    manager=EdgeModelManager(cloud,tmp_path/"models")
    status=manager.provision()
    assert status["ready"] is True
    path=manager.active_path("person-detection")
    assert path and path.read_bytes()==b"verified-model"
    assert cloud.downloads==1
    manager.provision()
    assert cloud.downloads==1


def test_model_manager_rejects_corrupt_download(tmp_path):
    cloud=FakeCloud(b"tampered",sha="0"*64)
    manager=EdgeModelManager(cloud,tmp_path/"models")
    with pytest.raises(RuntimeError,match="SHA-256 mismatch"):
        manager.provision()
    assert manager.active_path("person-detection") is None


def test_model_manager_offline_keeps_verified_cache(tmp_path):
    cloud=FakeCloud(b"verified-model")
    manager=EdgeModelManager(cloud,tmp_path/"models")
    manager.provision()
    cloud.enabled=lambda: False
    status=manager.provision()
    assert status["ready"] is True
    assert manager.active_path("person-detection").is_file()
