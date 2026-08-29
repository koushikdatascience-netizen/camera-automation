from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any


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

    def __init__(self, store, cloud_client, edge_config, sync_config, license_manager):
        self.store = store
        self.cloud_client = cloud_client
        self.edge_config = edge_config
        self.sync_config = sync_config
        self.license_manager = license_manager
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
            for row in self.store.queued_events(getattr(self.sync_config, "batch_size", 50)):
                try:
                    event = json.loads(row["payload_json"])
                    self.cloud_client.post_event(self.edge_config, event)
                    self.store.mark_event_synced(row["id"])
                    synced += 1
                except Exception as exc:
                    self.store.mark_event_failed(row["id"], str(exc))
                    failed += 1

            result = SyncRunResult(enabled=True, synced=synced, failed=failed)
            self._remember(result)
            return result

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
