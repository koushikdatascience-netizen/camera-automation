"""A saved camera owns its pipeline; HTTP viewers consume its latest output."""
import threading
import time


class SharedCameraWorker:
    def __init__(self, camera, pipeline, status_callback):
        self.camera = camera
        self.pipeline = pipeline
        self.status_callback = status_callback
        self.stop_event = threading.Event()
        self._attempt_stop = threading.Event()
        self._changed = threading.Condition()
        self._raw = None
        self._annotated = None
        self._sequence = 0

    def publish(self, raw, annotated):
        with self._changed:
            self._raw = raw
            self._annotated = annotated
            self._sequence += 1
            self._changed.notify_all()

    def snapshot(self, overlay=False):
        with self._changed:
            return self._annotated if overlay else self._raw

    def stream(self, overlay=True):
        sequence = -1
        while not self.stop_event.is_set():
            with self._changed:
                self._changed.wait_for(lambda: sequence != self._sequence or self.stop_event.is_set(), timeout=2)
                if self.stop_event.is_set():
                    return
                jpeg = self._annotated if overlay else self._raw
                if jpeg is None or sequence == self._sequence:
                    continue
                sequence = self._sequence
            yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + jpeg + b"\r\n"

    def run(self):
        retries = 0
        while not self.stop_event.is_set():
            self._attempt_stop = threading.Event()
            self.status_callback(self.camera.camera_id, state="STARTING", online=False)
            started = time.monotonic()
            try:
                for _ in self.pipeline(self.camera, self._attempt_stop, self.publish):
                    if self.stop_event.is_set():
                        break
                error = "Camera stopped delivering frames"
            except Exception:
                error = "Camera pipeline failed; check local diagnostics"
            if self.stop_event.is_set():
                break
            retries = 1 if time.monotonic() - started > 30 else min(retries + 1, 5)
            self.status_callback(self.camera.camera_id, state="RECONNECTING", online=False,
                                 reconnect_count=retries, last_error=error)
            with self._changed:
                self._raw = self._annotated = None
            self.stop_event.wait(min(30, 2 ** retries))
        self.status_callback(self.camera.camera_id, state="STOPPED", online=False)

    def stop(self):
        self.stop_event.set()
        self._attempt_stop.set()
        with self._changed:
            self._changed.notify_all()
