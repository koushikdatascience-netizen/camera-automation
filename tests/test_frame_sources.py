from pathlib import Path

import pytest

from camera_service.frame_sources import OpenCVFrameSource


def test_missing_video_fails_clearly(tmp_path):
    missing = tmp_path / "missing.mp4"
    source = OpenCVFrameSource(str(missing))
    with pytest.raises(FileNotFoundError, match="Video source does not exist"):
        source.open()


def test_read_before_open_fails():
    with pytest.raises(RuntimeError, match="not open"):
        OpenCVFrameSource(0).read()
