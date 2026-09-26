from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Any
from camera_service.camera.onvif import probe_onvif, select_profile


@dataclass
class SyncRunResult:
    enabled: bool
    blocked: bool = False
    synced: int = 0
    failed: int = 0
    message: str = ""

    def model_dump(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "blocked": self.blocked,
            "synced": self.synced,
            "failed": self.failed,
            "message": self.message,
        }


class EdgeSyncWorker:
    """Best-effort local queue drain for cloud portal/mobile visibility."""

    def __init__(self, store, cloud_client, edge_config, sync_config, license_manager, camera_manager=None):
        self.store = store
        self.cloud_client = cloud_client
        self.edge_config = edge_config
        self.sync_config = sync_config
        self.license_manager = license_manager
        self.camera_manager = camera_manager
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self.last_run_at: str | None = None
        self.last_result: dict[str, Any] | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="edge-sync-worker", daemon=True)
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            delay = max(2.0, float(getattr(self.sync_config, "interval_seconds", 15.0)))
            self._stop.wait(delay)

    def run_once(self) -> SyncRunResult:
        with self._lock:
            if not self.cloud_client.enabled():
                result = SyncRunResult(enabled=False, message="Cloud sync is disabled.")
                self._remember(result)
                return result

            license_status = self.license_manager.status()
            if not license_status.active:
                result = SyncRunResult(enabled=True, blocked=True, message=license_status.reason)
                self._remember(result, {"license": license_status.model_dump()})
                return result

            synced = 0
            failed = 0
            command_sync = {"fetched": 0, "completed": 0, "failed": 0}
            try:
                commands = self.cloud_client.edge_commands()
                command_sync["fetched"] = len(commands)
                for command in commands:
                    try:
                        result = self._execute_command(command)
                        self.cloud_client.complete_edge_command(command["id"], {"ok": True, **result})
                        command_sync["completed"] += 1
                    except Exception as exc:
                        self.cloud_client.complete_edge_command(command["id"], {"ok": False, "error": str(exc)})
                        command_sync["failed"] += 1
            except Exception as exc:
                command_sync["error"] = str(exc)
            camera_sync = {"fetched": 0, "applied": 0, "failed": 0}
            if self.camera_manager is not None:
                try:
                    assignment = self.cloud_client.camera_config()
                    items = assignment.get("items") or []
                    camera_sync["fetched"] = len(items)
                    for camera in items:
                        try:
                            self.camera_manager.apply_cloud_camera(camera)
                            camera_sync["applied"] += 1
                        except Exception:
                            camera_sync["failed"] += 1
                except Exception as exc:
                    camera_sync["error"] = str(exc)
            for row in self.store.queued_events(getattr(self.sync_config, "batch_size", 50)):
                try:
                    event = json.loads(row["payload_json"])
                    self.cloud_client.post_event(self.edge_config, event)
                    self.store.mark_event_synced(row["id"])
                    synced += 1
                except Exception as exc:
                    attempts = int(row.get("attempts", 0) or 0) + 1
                    retry_after = min(300.0, max(2.0, 2.0 ** min(attempts, 8)))
                    self.store.mark_event_failed(row["id"], str(exc), retry_after_seconds=retry_after)
                    failed += 1

            result = SyncRunResult(enabled=True, synced=synced, failed=failed)
            self._remember(result, {"camera_sync": camera_sync, "command_sync": command_sync})
            return result

    def _execute_command(self, command: dict[str, Any]) -> dict[str, Any]:
        command_type=str(command.get("command_type") or "")
        request=command.get("request") or {}
        if command_type=="ONVIF_PROBE":
            result=probe_onvif(str(request.get("host") or ""),int(request.get("port") or 80),
                               str(request.get("username") or ""),str(request.get("password") or ""))
            result["recommended_profile"]=select_profile(result.get("profiles") or [],str(request.get("purpose") or "ai"))
            return result
        if command_type=="CAMERA_TEST":
            if self.camera_manager is None: raise RuntimeError("camera manager is unavailable")
            source=str(request.get("source") or "")
            result=self.camera_manager.test_rtsp_connection(source)
            return result if isinstance(result,dict) else {"connected":bool(result)}
        raise RuntimeError(f"Unsupported edge command: {command_type}")

    def status(self) -> dict[str, Any]:
        return {
            "running": bool(self._thread and self._thread.is_alive()),
            "enabled": self.cloud_client.enabled(),
            "last_run_at": self.last_run_at,
            "last_result": self.last_result,
        }

    def _remember(self, result: SyncRunResult, extra: dict[str, Any] | None = None) -> None:
        data = result.model_dump()
        if extra:
            data.update(extra)
        self.last_result = data
        self.last_run_at = self.store.now()
