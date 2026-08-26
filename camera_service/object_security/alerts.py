from __future__ import annotations

import sys
import time


class ObjectSecurityAlerter:
    def __init__(self):
        self._last_sent: dict[str, float] = {}
        self.last_error: str | None = None

    def should_alert(self, key: str, cooldown_seconds: float = 15.0) -> bool:
        now = time.monotonic()
        last = self._last_sent.get(key, 0.0)
        if now - last < float(cooldown_seconds):
            return False
        self._last_sent[key] = now
        return True

    def beep(self, enabled: bool = True, frequency: int = 880, duration_ms: int = 400) -> bool:
        if not enabled:
            return False
        try:
            if sys.platform.startswith("win"):
                import winsound

                winsound.Beep(int(frequency), int(duration_ms))
                self.last_error = None
                return True
            self.last_error = "Native PC beep is only available on Windows."
            return False
        except Exception as exc:
            self.last_error = str(exc)
            return False
