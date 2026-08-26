from __future__ import annotations


def tiles_for_shape(width: int, height: int, tile_size: int = 640, overlap: float = 0.20):
    tile_size = max(64, int(tile_size))
    overlap = max(0.0, min(float(overlap), 0.8))
    step = max(1, int(tile_size * (1.0 - overlap)))
    y = 0
    while y < height:
        x = 0
        y2 = min(height, y + tile_size)
        y1 = max(0, y2 - tile_size)
        while x < width:
            x2 = min(width, x + tile_size)
            x1 = max(0, x2 - tile_size)
            yield (x1, y1, x2, y2)
            if x2 >= width:
                break
            x += step
        if y2 >= height:
            break
        y += step


def iou(a, b) -> float:
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    denom = area_a + area_b - inter
    return 0.0 if denom <= 0 else inter / denom


def suppress_duplicates(detections: list[dict], threshold: float = 0.45) -> list[dict]:
    kept: list[dict] = []
    for det in sorted(detections, key=lambda item: item.get("confidence", 0.0), reverse=True):
        if all(iou(det["bbox"], existing["bbox"]) < threshold for existing in kept):
            kept.append(det)
    return kept
