"""Camera orientation shared by all edge processing paths (clockwise degrees)."""
import cv2


def rotate_frame(frame, rotation_degrees=0):
    codes = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180,
             270: cv2.ROTATE_90_COUNTERCLOCKWISE}
    if rotation_degrees == 0:
        return frame
    if rotation_degrees not in codes:
        raise ValueError('rotation_degrees must be 0, 90, 180 or 270')
    return cv2.rotate(frame, codes[rotation_degrees])
