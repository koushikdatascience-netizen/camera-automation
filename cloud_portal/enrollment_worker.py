"""Bounded single-flight refresh jobs. Biometric payloads stay out of the queue."""
from concurrent.futures import ThreadPoolExecutor
import threading
import time


class EnrollmentWorker:
    def __init__(self, concurrency=1, capacity=32, retry_seconds=5):
        self.executor = ThreadPoolExecutor(max_workers=max(1, concurrency), thread_name_prefix="crm-enrollment")
        self.capacity = max(1, capacity)
        self.retry_seconds = retry_seconds
        self.lock = threading.Lock()
        self.pending = set()
        self.retry_at = {}
        self.closed = False
        self.metrics = {"completed": 0, "failed": 0, "processing_seconds": 0.0}

    def submit(self, key, operation):
        with self.lock:
            if self.closed or key in self.pending or len(self.pending) >= self.capacity:
                return False
            failures, deadline = self.retry_at.get(key, (0, 0))
            if time.monotonic() < deadline:
                return False
            self.pending.add(key)
        def run():
            started = time.monotonic()
            try:
                operation()
            except Exception:
                # Only aggregate status; exceptions can contain upstream secrets.
                with self.lock:
                    self.metrics["failed"] += 1
                    self.retry_at[key] = (failures + 1, time.monotonic() + min(300, self.retry_seconds * 2 ** min(failures, 6)))
            else:
                with self.lock:
                    self.metrics["completed"] += 1
                    self.retry_at.pop(key, None)
            finally:
                with self.lock:
                    self.pending.discard(key)
                    self.metrics["processing_seconds"] += time.monotonic() - started
        with self.lock:
            if self.closed:
                self.pending.discard(key)
                return False
            self.executor.submit(run)
        return True

    def status(self):
        with self.lock:
            return {**self.metrics, "queue_depth": len(self.pending)}

    def shutdown(self):
        with self.lock:
            self.closed = True
        self.executor.shutdown(wait=True, cancel_futures=True)
        with self.lock:
            self.pending.clear()
