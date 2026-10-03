import numpy as np
import pytest
from camera_service.orientation import rotate_frame
from camera_service.camera_manager import CameraManager, CameraConfig


@pytest.mark.parametrize('degrees,k', [(0, 0), (90, -1), (180, 2), (270, 1)])
def test_clockwise_rotation(degrees, k):
    frame = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
    assert np.array_equal(rotate_frame(frame, degrees), np.rot90(frame, k))


def test_default_and_persistence(tmp_path):
    manager = CameraManager(str(tmp_path / 'camera.db'))
    camera = manager.create_camera(dict(camera_id='webcam', name='Webcam', rtsp_url='0'))
    assert camera.rotation_degrees == 0
    for rotation in (90, 180, 270, 0):
        manager.update_camera('webcam', {'rotation_degrees': rotation})
        assert manager.get_camera('webcam').rotation_degrees == rotation
        assert manager.list_cameras()[0].rotation_degrees == rotation
    with pytest.raises(ValueError):
        manager.update_camera('webcam', {'rotation_degrees': 45})


def test_rotation_precedes_worker_tracking(tmp_path):
    from camera_service.camera.worker import CameraWorker
    from types import SimpleNamespace
    frame = np.arange(18, dtype=np.uint8).reshape(2, 3, 3)
    seen = []
    worker = CameraWorker.__new__(CameraWorker)
    worker.camera = SimpleNamespace(rotation_degrees=90)
    worker.source = SimpleNamespace(open=lambda: True, read=lambda: (True, frame))
    worker.tracker = SimpleNamespace(track_frame=lambda f: seen.append(f.copy()) or [])
    worker.process_tracks = lambda f, tracks: seen.append(f.copy())
    assert worker.run_once()
    assert all(np.array_equal(f, np.rot90(frame, -1)) for f in seen)


def test_legacy_database_migrates_to_zero(tmp_path):
    import sqlite3
    path = str(tmp_path / 'legacy.db')
    manager = CameraManager(path)
    manager.create_camera(dict(camera_id='old', name='Old', rtsp_url='0'))
    with sqlite3.connect(path) as conn:
        conn.execute('ALTER TABLE cameras DROP COLUMN rotation_degrees')
    assert CameraManager(path).get_camera('old').rotation_degrees == 0


def test_shared_pipeline_rotates_before_ai_and_publishes_same_orientation(tmp_path, monkeypatch):
    import threading
    import cv2
    manager = CameraManager(str(tmp_path / 'pipeline.db'))
    camera = CameraConfig(camera_id='cam', name='Camera', rtsp_url='test', source_type='file', rotation_degrees=90)
    frame = np.zeros((24, 40, 3), dtype=np.uint8)
    frame[:12, :20] = 255
    stop = threading.Event()
    seen, published = [], []
    class Capture:
        def isOpened(self): return True
        def read(self): return True, frame.copy()
        def release(self): pass
    monkeypatch.setattr(manager, '_open_video_capture', lambda *a: Capture())
    monkeypatch.setattr(manager, '_annotate_tracking_frame', lambda f, *a: seen.append(f.copy()) or f)
    monkeypatch.setattr(manager, '_draw_tracking_demo_overlay', lambda f, *a: f)
    iterator = manager.iter_tracking_mjpeg(camera, 'fake', stop_event=stop,
        publish_callback=lambda raw, annotated: published.append(cv2.imdecode(np.frombuffer(raw, np.uint8), cv2.IMREAD_COLOR)))
    try:
        next(iterator)
        assert published[0].shape == (40, 24, 3)
        assert seen and np.array_equal(seen[0], np.rot90(frame, -1))
    finally:
        stop.set(); iterator.close()
