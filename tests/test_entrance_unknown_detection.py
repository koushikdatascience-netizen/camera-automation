"""Regression tests for entrance/exit attendance + security feature coexistence."""

from camera_service.camera_manager import CameraConfig, CameraFeatures, CameraRole


def test_entrance_exit_preserves_enabled_unknown_detection():
    camera = CameraConfig(
        camera_id="entrance-test", name="Entrance", rtsp_url="0",
        source_type="webcam", camera_role=CameraRole.ENTRANCE_EXIT,
        features=CameraFeatures(unknown_detection=True, unknown_person_detection=True),
    )
    assert camera.features.attendance is True
    assert camera.features.face_recognition is True
    assert camera.tracking_mode == "track"
    assert camera.features.unknown_detection is True
    assert camera.features.unknown_person_detection is True


def test_entrance_exit_preserves_disabled_unknown_detection():
    camera = CameraConfig(
        camera_id="entrance-test-disabled", name="Entrance", rtsp_url="0",
        camera_role=CameraRole.ENTRANCE_EXIT,
        features=CameraFeatures(unknown_detection=False, unknown_person_detection=False),
    )
    assert camera.features.unknown_detection is False
    assert camera.features.unknown_person_detection is False
    assert camera.features.attendance is True
    assert camera.features.face_recognition is True
