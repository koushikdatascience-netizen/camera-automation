from __future__ import annotations


def apply_roi(frame, roi: dict | None):
    if not roi or not roi.get("enabled"):
        return frame, (0, 0)
    h, w = frame.shape[:2]
    x1 = max(0, min(int(roi.get("x1", 0)), w - 1))
    y1 = max(0, min(int(roi.get("y1", 0)), h - 1))
    x2 = max(x1 + 1, min(int(roi.get("x2", w)), w))
    y2 = max(y1 + 1, min(int(roi.get("y2", h)), h))
    return frame[y1:y2, x1:x2], (x1, y1)


def map_box_from_offset(box, offset):
    ox, oy = offset
    x1, y1, x2, y2 = box
    return (float(x1 + ox), float(y1 + oy), float(x2 + ox), float(y2 + oy))
