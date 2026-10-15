"""Frame-source adapters for repeatable development and production capture."""

from .base import FramePacket, FrameSource
from .opencv_source import OpenCVFrameSource

__all__ = ["FramePacket", "FrameSource", "OpenCVFrameSource"]
