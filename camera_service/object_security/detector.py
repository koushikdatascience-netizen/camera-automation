from __future__ import annotations

from pathlib import Path


class ObjectSecurityDetector:
    """Lazy Ultralytics loader for externally supplied object-security models."""

    def __init__(self):
        self._model_path: str | None = None
        self._model = None
        self.last_error: str | None = None

    def unload(self) -> None:
        self._model_path = None
        self._model = None

    def validate_model(self, model_path: str | Path) -> dict:
        model_path = Path(model_path)
        if model_path.suffix.lower() != ".pt":
            raise ValueError("Model file must be a .pt file.")
        if not model_path.exists():
            raise FileNotFoundError("Model file was not found.")
        model = self._load_ultralytics(model_path)
        names = getattr(model, "names", None)
        return {"ok": True, "classes": names or {}}

    def predict(self, model_path: str | Path, frame, conf: float = 0.30, imgsz: int = 960):
        model_path = str(Path(model_path))
        if self._model is None or self._model_path != model_path:
            self._model = self._load_ultralytics(Path(model_path))
            self._model_path = model_path
        return self._model.predict(frame, conf=conf, imgsz=imgsz, verbose=False)

    def _load_ultralytics(self, model_path: Path):
        try:
            from ultralytics import YOLO

            model = YOLO(str(model_path))
            self.last_error = None
            return model
        except Exception as exc:
            self.last_error = str(exc)
            raise
