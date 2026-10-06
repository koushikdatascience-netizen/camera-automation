import hashlib
from pathlib import Path

from camera_service.updater import EdgeUpdater


def test_updater_verifies_installer_before_launch(tmp_path, monkeypatch):
    payload=b"trusted-installer-bytes"
    digest=hashlib.sha256(payload).hexdigest()
    launched=[]

    class Cloud:
        def enabled(self): return True
        def latest_update(self, current):
            return {"available":True,"version":"1.0.99","build_id":"build-99","sha256":digest}
        def download_update(self, build_id, destination):
            Path(destination).write_bytes(payload)

    monkeypatch.setenv("CAMERA_AUTOMATION_HOME",str(tmp_path))
    monkeypatch.setattr("camera_service.updater.subprocess.Popen",lambda args,**kwargs: launched.append(args))
    result=EdgeUpdater(Cloud()).check_and_stage()
    assert result["staged"] is True
    assert (tmp_path/"updates"/"build-99"/"ready.txt").is_file()
    assert launched and "RunAs" in launched[0][5]


def test_updater_rejects_tampered_installer(tmp_path, monkeypatch):
    class Cloud:
        def enabled(self): return True
        def latest_update(self, current):
            return {"available":True,"version":"1.0.100","build_id":"bad-build","sha256":"0"*64}
        def download_update(self, build_id, destination):
            Path(destination).write_bytes(b"tampered")

    monkeypatch.setenv("CAMERA_AUTOMATION_HOME",str(tmp_path))
    monkeypatch.setattr("camera_service.updater.subprocess.Popen",lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError("must not launch")))
    try:
        EdgeUpdater(Cloud()).check_and_stage()
        raise AssertionError("tampered installer was accepted")
    except RuntimeError as exc:
        assert "SHA-256" in str(exc)
    assert not (tmp_path/"updates"/"bad-build"/"MadhushalaCameraAISetup.exe").exists()
