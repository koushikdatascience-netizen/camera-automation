from __future__ import annotations

import json
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class ObjectSecurityModelRegistry:
    """Writable registry for externally trained object-security YOLO models."""

    def __init__(self, root: str | Path):
        self.root = Path(root)

    def class_root(self, object_class: str, create: bool = False) -> Path:
        safe = self._safe_name(object_class)
        path = self.root / safe
        if create:
            path.mkdir(parents=True, exist_ok=True)
            (path / "candidates").mkdir(parents=True, exist_ok=True)
            (path / "production").mkdir(parents=True, exist_ok=True)
        return path

    def registry_path(self, object_class: str, create: bool = False) -> Path:
        return self.class_root(object_class, create=create) / "registry.json"

    def read_registry(self, object_class: str) -> dict[str, Any]:
        path = self.registry_path(object_class)
        if not path.exists():
            return {"object_class": self._safe_name(object_class), "active": None, "previous": None, "updated_at": None}
        return json.loads(path.read_text(encoding="utf-8"))

    def write_registry(self, object_class: str, registry: dict[str, Any]) -> dict[str, Any]:
        registry["object_class"] = self._safe_name(object_class)
        registry["updated_at"] = self._now()
        path = self.registry_path(object_class, create=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return registry

    def list_models(self, object_class: str = "scissors") -> dict[str, Any]:
        root = self.class_root(object_class)
        registry = self.read_registry(object_class)
        candidates = []
        candidates_root = root / "candidates"
        if candidates_root.exists():
            for candidate_dir in sorted(candidates_root.iterdir()):
                if not candidate_dir.is_dir():
                    continue
                candidates.append({
                    "id": candidate_dir.name,
                    "metadata": self._read_metadata(candidate_dir / "metadata.json"),
                    "has_model": (candidate_dir / "best.pt").exists(),
                    "status": "CANDIDATE",
                })
        return {
            "object_class": self._safe_name(object_class),
            "active": self._model_ref(object_class, registry.get("active"), "production"),
            "previous": self._model_ref(object_class, registry.get("previous"), "production"),
            "candidates": candidates,
            "missing_production": registry.get("active") is None,
        }

    def candidate_path(self, object_class: str, candidate_id: str) -> Path:
        return self.class_root(object_class) / "candidates" / self._safe_name(candidate_id)

    def production_path(self, object_class: str, model_id: str) -> Path:
        return self.class_root(object_class) / "production" / self._safe_name(model_id)

    def create_candidate(self, object_class: str, upload_path: Path, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        if upload_path.suffix.lower() != ".pt":
            raise ValueError("Only externally trained .pt models are accepted.")
        metadata = metadata or {}
        candidate_id = f"{self._now_compact()}-{uuid.uuid4().hex[:8]}"
        self.class_root(object_class, create=True)
        dest_dir = self.candidate_path(object_class, candidate_id)
        dest_dir.mkdir(parents=True, exist_ok=False)
        shutil.copy2(upload_path, dest_dir / "best.pt")
        final_metadata = {
            "id": candidate_id,
            "object_class": self._safe_name(object_class),
            "model_name": metadata.get("model_name"),
            "version": metadata.get("version") or candidate_id,
            "source": metadata.get("source"),
            "dataset_notes": metadata.get("dataset_notes"),
            "precision": metadata.get("precision"),
            "recall": metadata.get("recall"),
            "map50": metadata.get("map50"),
            "map50_95": metadata.get("map50_95"),
            "uploaded_at": self._now(),
        }
        self._write_metadata(dest_dir / "metadata.json", final_metadata)
        return {"id": candidate_id, "metadata": final_metadata, "has_model": True, "status": "CANDIDATE"}

    def activate_candidate(self, object_class: str, candidate_id: str, validator=None) -> dict[str, Any]:
        candidate_dir = self.candidate_path(object_class, candidate_id)
        source_model = candidate_dir / "best.pt"
        if not source_model.exists():
            raise FileNotFoundError("Candidate model best.pt was not found.")
        if validator:
            validator(source_model)

        registry = self.read_registry(object_class)
        model_id = self._safe_name(candidate_id)
        self.class_root(object_class, create=True)
        dest_dir = self.production_path(object_class, model_id)
        tmp_dir = dest_dir.with_name(f".{model_id}.tmp")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_model, tmp_dir / "best.pt")
        shutil.copy2(candidate_dir / "metadata.json", tmp_dir / "metadata.json")
        if dest_dir.exists():
            shutil.rmtree(dest_dir)
        tmp_dir.replace(dest_dir)

        registry["previous"] = registry.get("active")
        registry["active"] = model_id
        self.write_registry(object_class, registry)
        return self.list_models(object_class)

    def rollback(self, object_class: str, validator=None) -> dict[str, Any]:
        registry = self.read_registry(object_class)
        previous = registry.get("previous")
        if not previous:
            raise ValueError("No previous production model is available for rollback.")
        model_path = self.production_path(object_class, previous) / "best.pt"
        if not model_path.exists():
            raise FileNotFoundError("Previous production model file is missing.")
        if validator:
            validator(model_path)
        registry["active"], registry["previous"] = registry.get("previous"), registry.get("active")
        self.write_registry(object_class, registry)
        return self.list_models(object_class)

    def delete_candidate(self, object_class: str, candidate_id: str) -> bool:
        candidate_dir = self.candidate_path(object_class, candidate_id)
        if not candidate_dir.exists():
            return False
        shutil.rmtree(candidate_dir)
        return True

    def active_model_path(self, object_class: str = "scissors") -> Path | None:
        active = self.read_registry(object_class).get("active")
        if not active:
            return None
        path = self.production_path(object_class, active) / "best.pt"
        return path if path.exists() else None

    def _model_ref(self, object_class: str, model_id: str | None, group: str) -> dict[str, Any] | None:
        if not model_id:
            return None
        base = self.class_root(object_class) / group / self._safe_name(model_id)
        return {
            "id": self._safe_name(model_id),
            "metadata": self._read_metadata(base / "metadata.json"),
            "has_model": (base / "best.pt").exists(),
            "status": "PRODUCTION" if group == "production" else group.upper(),
        }

    @staticmethod
    def _read_metadata(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))

    @staticmethod
    def _write_metadata(path: Path, metadata: dict[str, Any]) -> None:
        path.write_text(json.dumps(metadata, indent=2, sort_keys=True), encoding="utf-8")

    @staticmethod
    def _safe_name(value: str) -> str:
        cleaned = "".join(ch for ch in str(value).strip().lower() if ch.isalnum() or ch in {"-", "_"})
        if not cleaned or cleaned in {".", ".."}:
            raise ValueError("Invalid object/model identifier.")
        return cleaned

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _now_compact() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
