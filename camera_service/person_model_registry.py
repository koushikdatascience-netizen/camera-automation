from __future__ import annotations

import os
import platform
from pathlib import Path
from typing import Any


def _first_existing(candidates: list[Path]) -> Path | None:
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def detect_edge_hardware() -> dict[str, Any]:
    info: dict[str, Any] = {
        "system": platform.system(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count() or 1,
        "cuda": False,
        "openvino": False,
        "onnxruntime": False,
    }
    try:
        import torch
        info["cuda"] = bool(torch.cuda.is_available())
        if info["cuda"]:
            info["cuda_device"] = torch.cuda.get_device_name(0)
    except Exception:
        pass
    try:
        import openvino  # noqa: F401
        info["openvino"] = True
    except Exception:
        pass
    try:
        import onnxruntime  # noqa: F401
        info["onnxruntime"] = True
    except Exception:
        pass
    return info


class PersonModelRegistry:
    """Resolve the best locally available person detector artifact.

    Explicit SNAPKEY_PERSON_MODEL always wins. Otherwise CPU edges prefer
    OpenVINO -> ONNX -> PyTorch. CUDA edges keep PyTorch first so Ultralytics
    can use CUDA without forcing a CPU exported artifact.
    """

    def __init__(self, app_config):
        self.config = app_config.runtime.person_model
        self.hardware = detect_edge_hardware()
        self.selection: dict[str, Any] | None = None

    def _roots(self) -> list[Path]:
        roots = [Path.cwd(), Path(self.config.storage_dir)]
        try:
            import sys
            if getattr(sys, "frozen", False):
                roots.insert(0, Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)))
        except Exception:
            pass
        return roots

    def _candidates(self, artifact: str) -> list[Path]:
        value = Path(artifact)
        if value.is_absolute():
            return [value]
        return [root / value for root in self._roots()]

    @staticmethod
    def _diagnostic_disabled() -> set[str]:
        """Test-only runtime exclusions; never moves/deletes model artifacts."""
        raw = os.environ.get("SNAPKEY_PERSON_DIAGNOSTIC_DISABLE", "")
        return {item.strip().upper() for item in raw.split(",") if item.strip().upper() in {"OPENVINO", "ONNX", "PYTORCH"}}

    def resolve(self) -> dict[str, Any]:
        disabled = self._diagnostic_disabled()
        override = os.environ.get("SNAPKEY_PERSON_MODEL", "").strip()
        if override:
            path = Path(override)
            if not path.exists():
                raise FileNotFoundError(f"SNAPKEY_PERSON_MODEL does not exist: {path}")
            runtime = self._runtime_for(path)
            return self._set(path, runtime, "environment_override")

        preference = os.environ.get("SNAPKEY_PERSON_RUNTIME", self.config.preferred_runtime).strip().upper() or "AUTO"
        artifacts = {
            "OPENVINO": self.config.openvino_artifact,
            "ONNX": self.config.onnx_artifact,
            "PYTORCH": self.config.pytorch_artifact,
        }
        if preference != "AUTO":
            order = [preference, "OPENVINO", "ONNX", "PYTORCH"]
        elif self.hardware.get("cuda"):
            order = ["PYTORCH", "OPENVINO", "ONNX"]
        else:
            order = ["OPENVINO", "ONNX", "PYTORCH"]

        seen = set()
        for runtime in order:
            if runtime in seen or runtime not in artifacts or runtime in disabled:
                continue
            seen.add(runtime)
            if runtime == "OPENVINO" and not self.hardware.get("openvino"):
                continue
            if runtime == "ONNX" and not self.hardware.get("onnxruntime"):
                continue
            path = _first_existing(self._candidates(artifacts[runtime]))
            if path is not None:
                return self._set(path, runtime, "automatic")

        # Preserve the existing resolved PT model as the final offline fallback.
        legacy = Path(self.config.pytorch_artifact)
        raise FileNotFoundError(
            f"No usable person model artifact found. Expected OpenVINO/ONNX/PT for {legacy.stem}."
        )

    def _runtime_for(self, path: Path) -> str:
        if path.is_dir() or path.suffix.lower() == ".xml":
            return "OPENVINO"
        if path.suffix.lower() == ".onnx":
            return "ONNX"
        return "PYTORCH"

    def _set(self, path: Path, runtime: str, reason: str) -> dict[str, Any]:
        self.selection = {
            "model_id": self.config.model_id,
            "family": self.config.family,
            "version": self.config.version,
            "runtime": runtime,
            "path": str(path.resolve()),
            "reason": reason,
            "diagnostic_disabled": sorted(self._diagnostic_disabled()),
            "hardware": self.hardware,
        }
        return self.selection
