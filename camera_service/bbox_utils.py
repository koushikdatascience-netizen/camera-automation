from __future__ import annotations
from typing import Iterable
import numpy as np

BBox = tuple[float, float, float, float]  # x1,y1,x2,y2

def normalize_xyxy(box: Iterable[float]) -> BBox:
    vals = tuple(float(v) for v in box)
    if len(vals) != 4:
        raise ValueError("bbox must contain four values")
    x1,y1,x2,y2 = vals
    if x2 <= x1 or y2 <= y1:
        raise ValueError("invalid xyxy bbox")
    return x1,y1,x2,y2

def width(box: BBox) -> float: return box[2]-box[0]
def height(box: BBox) -> float: return box[3]-box[1]
def area(box: BBox) -> float: return width(box)*height(box)

def clip(box: BBox, frame_shape) -> BBox:
    h,w = frame_shape[:2]
    x1,y1,x2,y2 = box
    x1=max(0,min(w-1,x1)); x2=max(1,min(w,x2)); y1=max(0,min(h-1,y1)); y2=max(1,min(h,y2))
    return normalize_xyxy((x1,y1,x2,y2))

def crop(frame: np.ndarray, box: BBox) -> np.ndarray:
    x1,y1,x2,y2 = clip(box, frame.shape)
    return frame[int(y1):int(y2), int(x1):int(x2)]

def bottom_center(box: BBox) -> tuple[float,float]:
    x1,y1,x2,y2=box
    return ((x1+x2)/2.0, y2)
