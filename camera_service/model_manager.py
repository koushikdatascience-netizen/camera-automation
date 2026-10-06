from __future__ import annotations

import hashlib
import json
import os
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


logger = logging.getLogger("camera_eye.models")


class EdgeModelManager:
    """Provision versioned AI artifacts from the authenticated Camera Eye registry."""

    def __init__(self, cloud_client, root: str | Path | None = None):
        home = Path(os.environ.get("CAMERA_AUTOMATION_HOME") or (Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "MadhushalaCameraAI"))
        self.root = Path(root) if root else home / "models"
        self.cloud = cloud_client
        self.state_path = self.root / "active.json"
        self.last_error: str | None = None
        self.last_stage = "idle"
        self.last_attempt_at: str | None = None
        self.last_success_at: str | None = None

    def state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"models": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"models": {}}
        except Exception:
            return {"models": {}}

    def status(self) -> dict[str, Any]:
        state = self.state()
        items = state.get("models") or {}
        return {
            "ready": all(not v.get("required") or v.get("ready") for v in items.values()),
            "models": items,
            "root": str(self.root),
            "state_path": str(self.state_path),
            "last_stage": self.last_stage,
            "last_attempt_at": self.last_attempt_at,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
        }

    def provision(self) -> dict[str, Any]:
        self.last_attempt_at = datetime.now(timezone.utc).isoformat()
        self.last_stage = "starting"
        if not self.cloud.enabled():
            self.last_stage = "cloud_disabled"
            logger.warning("Model provisioning skipped: cloud client is disabled")
            return self.status()
        logger.info("Model provisioning started; root=%s", self.root)
        self.last_stage = "fetch_manifest"
        manifest = self.cloud.model_manifest()
        logger.info("Model manifest received; models=%d", len(manifest.get("models") or []))
        state = self.state()
        models = state.setdefault("models", {})
        for item in manifest.get("models") or []:
            model_id = self._safe(item.get("id"))
            version = self._safe(item.get("version"))
            filename = Path(str(item.get("filename") or "model.bin")).name
            expected = str(item.get("sha256") or "").lower()
            if len(expected) != 64:
                raise RuntimeError(f"Invalid SHA-256 for model {model_id}")
            target_dir = self.root / model_id / version
            target = target_dir / filename
            current = models.get(model_id) or {}
            self.last_stage = f"prepare:{model_id}"
            if target.is_file() and self._sha256(target) == expected:
                logger.info("Using verified cached model %s version=%s path=%s", model_id, version, target)
                models[model_id] = self._entry(item, target, True)
                self._write_state(state)
                continue
            target_dir.mkdir(parents=True, exist_ok=True)
            temporary = target.with_suffix(target.suffix + ".part")
            temporary.unlink(missing_ok=True)
            self.last_stage = f"download:{model_id}"
            logger.info("Downloading model %s version=%s to %s", model_id, version, temporary)
            self.cloud.download_model(model_id, version, temporary)
            self.last_stage = f"verify:{model_id}"
            actual = self._sha256(temporary)
            if actual != expected:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f"SHA-256 mismatch for model {model_id}")
            temporary.replace(target)
            models[model_id] = self._entry(item, target, True, previous=current.get("path"))
            self._write_state(state)
            logger.info("Model verified and activated %s version=%s sha256=%s path=%s", model_id, version, actual, target)
        self.last_error = None
        self.last_stage = "ready"
        self.last_success_at = datetime.now(timezone.utc).isoformat()
        self._write_state(state)
        logger.info("Model provisioning completed successfully")
        return self.status()

    def active_path(self, model_id: str) -> Path | None:
        item = (self.state().get("models") or {}).get(model_id) or {}
        path = Path(item["path"]) if item.get("path") else None
        return path if path and path.is_file() else None

    def _entry(self, item: dict[str, Any], path: Path, ready: bool, previous: str | None = None) -> dict[str, Any]:
        return {"version": item.get("version"), "runtime": item.get("runtime"), "feature": item.get("feature"),
                "required": bool(item.get("required")), "sha256": item.get("sha256"), "path": str(path.resolve()),
                "previous_path": previous, "ready": ready}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.state_path)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _safe(value: Any) -> str:
        cleaned = "".join(ch for ch in str(value or "").strip().lower() if ch.isalnum() or ch in {"-", "_", "."})
        if not cleaned or cleaned in {".", ".."}:
            raise RuntimeError("Invalid model identifier")
        return cleaned
