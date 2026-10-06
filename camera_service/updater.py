from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from camera_service.build_info import BUILD_ID, VERSION


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class EdgeUpdater:
    """Background updater for frozen Windows Camera Eye installations."""

    def __init__(self, cloud_client, *, interval_seconds: float = 1800.0):
        self.cloud_client = cloud_client
        self.interval_seconds = max(300.0, float(interval_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_stage = "created"
        self.last_check_at: str | None = None
        self.last_result: dict[str, Any] | None = None
        self.last_error: str | None = None

    def status(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "build_id": BUILD_ID,
            "running": bool(self._thread and self._thread.is_alive()),
            "interval_seconds": self.interval_seconds,
            "last_stage": self.last_stage,
            "last_check_at": self.last_check_at,
            "last_result": self.last_result,
            "last_error": self.last_error,
        }

    def start(self) -> bool:
        reasons = []
        if os.name != "nt":
            reasons.append(f"os={os.name}")
        if not getattr(sys, "frozen", False):
            reasons.append("not_frozen")
        if not self.cloud_client.enabled():
            reasons.append("cloud_disabled")
        if reasons:
            self.last_stage = "disabled"
            self.last_error = ",".join(reasons)
            print(f"Camera Eye updater disabled: {self.last_error}", flush=True)
            return False
        if self._thread and self._thread.is_alive():
            print("Camera Eye updater already running", flush=True)
            return True
        self.last_stage = "starting"
        self._thread = threading.Thread(target=self._loop, name="camera-eye-updater", daemon=True)
        self._thread.start()
        print(
            f"Camera Eye updater started version={VERSION} build_id={BUILD_ID} "
            f"interval={self.interval_seconds}s initial_delay=120s",
            flush=True,
        )
        return True

    def stop(self) -> None:
        self.last_stage = "stopping"
        self._stop.set()
        print("Camera Eye updater stop requested", flush=True)

    def _loop(self) -> None:
        self.last_stage = "initial_delay"
        if self._stop.wait(120):
            return
        while not self._stop.is_set():
            try:
                self.last_stage = "checking"
                self.last_check_at = _now()
                print(f"Camera Eye updater checking current_build_id={BUILD_ID}", flush=True)
                result = self.check_and_stage()
                self.last_result = result
                self.last_error = None
                if result.get("available"):
                    self.last_stage = "installer_launched"
                    print(
                        f"Camera Eye updater staged update version={result.get('version')} "
                        f"build_id={result.get('build_id')}",
                        flush=True,
                    )
                    # Avoid repeatedly launching the same installer while this
                    # process waits for the elevated installer to replace it.
                    return
                self.last_stage = "idle"
                print("Camera Eye updater: no newer build available", flush=True)
            except Exception as exc:
                self.last_stage = "error"
                self.last_error = f"{type(exc).__name__}: {exc}"
                print(f"Camera Eye updater check failed: {self.last_error}", flush=True)
            self._stop.wait(self.interval_seconds)

    def check_and_stage(self) -> dict[str, Any]:
        manifest = self.cloud_client.latest_update(BUILD_ID)
        latest = str(manifest.get("build_id") or "")
        if not manifest.get("available") or not latest or latest == BUILD_ID:
            return {"available": False, "current_build_id": BUILD_ID}

        root = Path(
            os.environ.get("CAMERA_AUTOMATION_HOME")
            or Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "MadhushalaCameraAI"
        )
        update_dir = root / "updates" / latest
        update_dir.mkdir(parents=True, exist_ok=True)
        installer = update_dir / "MadhushalaCameraAISetup.exe"
        print(
            f"Camera Eye updater downloading version={manifest.get('version')} "
            f"build_id={latest} destination={installer}",
            flush=True,
        )
        self.cloud_client.download_update(latest, installer)
        expected = str(manifest.get("sha256") or "").lower()
        actual = hashlib.sha256(installer.read_bytes()).hexdigest().lower()
        if not expected or actual != expected:
            installer.unlink(missing_ok=True)
            raise RuntimeError("Downloaded Camera Eye update failed SHA-256 verification")
        print(f"Camera Eye updater SHA-256 verified build_id={latest}", flush=True)

        marker = update_dir / "ready.txt"
        marker.write_text(
            f"version={manifest.get('version')}\nbuild_id={latest}\nsha256={actual}\n",
            encoding="utf-8",
        )
        # Pass the installer path as a named PowerShell parameter instead of
        # relying on $args[0] after -Command. This keeps paths with spaces safe
        # and makes the elevation hand-off deterministic.
        ps_script = (
            "param([string]$Installer) "
            "Start-Process -FilePath $Installer "
            "-ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/NORESTART','/CLOSEAPPLICATIONS' "
            "-Verb RunAs"
        )
        command = [
            "powershell.exe",
            "-NoProfile",
            "-WindowStyle",
            "Hidden",
            "-Command",
            ps_script,
            "-Installer",
            str(installer),
        ]
        subprocess.Popen(command, close_fds=True)
        print(f"Camera Eye updater launched elevated installer build_id={latest}", flush=True)
        return {
            "available": True,
            "staged": True,
            "version": manifest.get("version"),
            "build_id": latest,
            "sha256": actual,
            "installer": str(installer),
        }


def current_build() -> dict[str, str]:
    return {"version": VERSION, "build_id": BUILD_ID}
