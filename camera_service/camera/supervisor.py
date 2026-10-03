from __future__ import annotations

import threading
from camera_service.camera.worker import CameraWorker
from camera_service.camera.shared import SharedCameraWorker


class CameraSupervisor:
    """Keeps enabled persisted/cloud cameras running independently of the browser."""

    def __init__(self, config, store, face, attendance, camera_manager=None, pipeline=None, allowed=None):
        self.config = config
        self.store = store
        self.face = face
        self.attendance = attendance
        self.camera_manager = camera_manager
        self.pipeline = pipeline
        self.allowed = allowed
        self._stop = threading.Event()
        self._monitor = None
        self.workers = {}
        self.threads = {}
        self._lock = threading.RLock()

    def _desired_cameras(self):
        if self.camera_manager is not None:
            return [camera for camera in self.camera_manager.list_cameras() if camera.enabled]
        return [camera for camera in self.config.cameras if camera.enabled]

    def _start_camera_locked(self, camera):
        current = self.threads.get(camera.camera_id)
        if current and current.is_alive():
            return False
        worker = SharedCameraWorker(camera, self.pipeline, self._status_callback) if self.pipeline else CameraWorker(
            self.config, camera, self.store, self.face, self.attendance,
            status_callback=self._status_callback,
        )
        thread = threading.Thread(
            target=worker.run, name=f"camera-{camera.camera_id}", daemon=True
        )
        self.workers[camera.camera_id] = worker
        self.threads[camera.camera_id] = thread
        thread.start()
        return True

    def _status_callback(self, camera_id, **changes):
        if self.camera_manager is None:
            return
        try:
            self.camera_manager.update_camera_status(camera_id, **changes)
        except Exception:
            pass

    def start(self):
        self.reconcile()
        if self._monitor and self._monitor.is_alive():
            return
        self._stop.clear()
        self._monitor = threading.Thread(target=self._monitor_loop, name="camera-supervisor", daemon=True)
        self._monitor.start()

    def _monitor_loop(self):
        while not self._stop.wait(2):
            try:
                self.reconcile()
            except Exception:
                continue

    def reconcile(self):
        """Start enabled saved cameras, restart changed ones, stop disabled/deleted ones."""
        with self._lock:
            desired = {camera.camera_id: camera for camera in self._desired_cameras()}
            sources = set()
            for camera_id, camera in list(desired.items()):
                if (self.allowed and not self.allowed(camera)) or camera.rtsp_url in sources:
                    desired.pop(camera_id)
                    continue
                sources.add(camera.rtsp_url)
            for camera_id in list(self.workers):
                if camera_id not in desired:
                    self._stop_camera_locked(camera_id)
            for camera_id, camera in desired.items():
                worker = self.workers.get(camera_id)
                thread = self.threads.get(camera_id)
                changed = worker is not None and worker.camera.model_dump() != camera.model_dump()
                if changed:
                    self._stop_camera_locked(camera_id)
                    if self.threads.get(camera_id) and self.threads[camera_id].is_alive():
                        continue
                    worker = None
                    thread = None
                if worker is None or thread is None or not thread.is_alive():
                    self._start_camera_locked(camera)

    def start_camera(self, camera_id):
        if self.camera_manager is None:
            return False
        camera = self.camera_manager.get_camera(camera_id)
        if not camera or not camera.enabled:
            return False
        with self._lock:
            return self._start_camera_locked(camera)

    def stop_camera(self, camera_id):
        with self._lock:
            return self._stop_camera_locked(camera_id)

    def restart_camera(self, camera_id):
        with self._lock:
            self._stop_camera_locked(camera_id)
            if self.camera_manager is None:
                return False
            camera = self.camera_manager.get_camera(camera_id)
            return bool(camera and camera.enabled and self._start_camera_locked(camera))

    def _stop_camera_locked(self, camera_id):
        worker = self.workers.get(camera_id)
        thread = self.threads.get(camera_id)
        if worker:
            worker.stop()
        if thread and thread.is_alive():
            thread.join(timeout=3)
        if not thread or not thread.is_alive():
            self.workers.pop(camera_id, None)
            self.threads.pop(camera_id, None)
        return bool(worker or thread)

    def shutdown(self):
        self._stop.set()
        if self._monitor:
            self._monitor.join(timeout=3)
        with self._lock:
            for camera_id in list(self.workers):
                self._stop_camera_locked(camera_id)

    def is_running(self):
        return bool(self._monitor and self._monitor.is_alive())

    def stream(self, camera_id, overlay=True):
        self.reconcile()
        with self._lock:
            worker = self.workers.get(camera_id)
        if worker and hasattr(worker, "stream"):
            yield from worker.stream(overlay)

    def snapshot(self, camera_id, overlay=False):
        with self._lock:
            worker = self.workers.get(camera_id)
        return worker.snapshot(overlay) if worker and hasattr(worker, "snapshot") else None

    def status(self):
        with self._lock:
            return {
                camera_id: {
                    "running": bool(self.threads.get(camera_id) and self.threads[camera_id].is_alive())
                }
                for camera_id in set(self.workers) | set(self.threads)
            }
