from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from camera_service.build_info import BUILD_ID, VERSION


class EdgeUpdater:
    """Background updater for frozen Windows Camera Eye installations."""

    def __init__(self, cloud_client, *, interval_seconds: float = 1800.0):
        self.cloud_client = cloud_client
        self.interval_seconds = max(300.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if os.name != "nt" or not getattr(sys, "frozen", False) or not self.cloud_client.enabled():
            return
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, name="camera-eye-updater", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        # Do not compete with model/service startup.
        self._stop.wait(120)
        while not self._stop.is_set():
            try:
                self.check_and_stage()
            except Exception as exc:
                print(f"Update check failed: {exc}")
            self._stop.wait(self.interval_seconds)

    def check_and_stage(self) -> dict[str, Any]:
        manifest = self.cloud_client.latest_update(BUILD_ID)
        latest = str(manifest.get("build_id") or "")
        if not manifest.get("available") or not latest or latest == BUILD_ID:
            return {"available": False, "current_build_id": BUILD_ID}
        root = Path(os.environ.get("CAMERA_AUTOMATION_HOME") or Path(os.environ.get("PROGRAMDATA", r"C:\\ProgramData")) / "MadhushalaCameraAI")
        update_dir = root / "updates" / latest
        update_dir.mkdir(parents=True, exist_ok=True)
        installer = update_dir / "MadhushalaCameraAISetup.exe"
        self.cloud_client.download_update(latest, installer)
        expected = str(manifest.get("sha256") or "").lower()
        actual = hashlib.sha256(installer.read_bytes()).hexdigest().lower()
        if not expected or actual != expected:
            installer.unlink(missing_ok=True)
            raise RuntimeError("Downloaded Camera Eye update failed SHA-256 verification")
        marker = update_dir / "ready.txt"
        marker.write_text(f"version={manifest.get('version')}\nbuild_id={latest}\nsha256={actual}\n", encoding="utf-8")
        # Inno requires elevation because the app lives in Program Files. ShellExecute
        # via PowerShell intentionally lets Windows show the trusted UAC consent UI.
        command = [
            "powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command",
            "Start-Process -FilePath $args[0] -ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS' -Verb RunAs",
            str(installer),
        ]
        subprocess.Popen(command, close_fds=True)
        return {"available": True, "staged": True, "version": manifest.get("version"), "build_id": latest}


def current_build() -> dict[str, str]:
    return {"version": VERSION, "build_id": BUILD_ID}
