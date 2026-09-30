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
