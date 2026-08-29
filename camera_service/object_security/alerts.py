from __future__ import annotations

import sys
import threading
import time


class ObjectSecurityAlerter:
    def __init__(self):
        self._last_sent: dict[str, float] = {}
        self._alarm_until: dict[str, float] = {}
        self._alarm_threads: dict[str, threading.Thread] = {}
        self._lock = threading.Lock()
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

    def beep_async(self, enabled: bool = True, frequency: int = 880, duration_ms: int = 400) -> bool:
        if not enabled:
            return False
        if not sys.platform.startswith("win"):
            self.last_error = "Native PC beep is only available on Windows."
            return False

        thread = threading.Thread(target=self.beep, args=(True, frequency, duration_ms), daemon=True)
        thread.start()
        return True

    def alarm_beep(
        self,
        key: str,
        enabled: bool = True,
        frequency: int = 1400,
        duration_ms: int = 160,
        hold_seconds: float = 1.0,
    ) -> bool:
        if not enabled:
            return False
        if not sys.platform.startswith("win"):
            self.last_error = "Native PC alarm is only available on Windows."
            return False

        with self._lock:
            self._alarm_until[key] = time.monotonic() + max(0.3, float(hold_seconds))
            thread = self._alarm_threads.get(key)
            if thread and thread.is_alive():
                return True
            thread = threading.Thread(
                target=self._alarm_loop,
                args=(key, int(frequency), int(duration_ms)),
                daemon=True,
            )
            self._alarm_threads[key] = thread
            thread.start()
            return True

    def _alarm_loop(self, key: str, frequency: int, duration_ms: int) -> None:
        try:
            import winsound

            while True:
                with self._lock:
                    until = self._alarm_until.get(key, 0.0)
                if time.monotonic() >= until:
                    break
                winsound.Beep(frequency, duration_ms)
                with self._lock:
                    until = self._alarm_until.get(key, 0.0)
                if time.monotonic() >= until:
                    break
                winsound.Beep(frequency + 350, max(80, duration_ms // 2))
                time.sleep(0.05)
            self.last_error = None
        except Exception as exc:
            self.last_error = str(exc)
