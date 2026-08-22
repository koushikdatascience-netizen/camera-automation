import argparse
import sys
import time

import cv2


def open_capture(source: str):
    source_text = str(source).strip()
    if source_text.isdigit():
        device_index = int(source_text)
        if sys.platform.startswith("win"):
            backends = [
                ("DSHOW", getattr(cv2, "CAP_DSHOW", None)),
                ("MSMF", getattr(cv2, "CAP_MSMF", None)),
                ("DEFAULT", None),
            ]
            fallback = None
            for name, backend in backends:
                cap = (
                    cv2.VideoCapture(device_index)
                    if backend is None
                    else cv2.VideoCapture(device_index, backend)
                )
                if fallback is None:
                    fallback = cap
                if not cap.isOpened():
                    print(f"{name}: not opened")
                    if cap is not fallback:
                        cap.release()
                    continue
                for _ in range(5):
                    ok, frame = cap.read()
                    if ok and frame is not None:
                        print(f"{name}: opened and receiving frames")
                        if fallback is not cap and fallback is not None:
                            fallback.release()
                        return cap
                    time.sleep(0.05)
                print(f"{name}: opened but no frames")
                if cap is not fallback:
                    cap.release()
            return fallback or cv2.VideoCapture(device_index)
        return cv2.VideoCapture(device_index)
    return cv2.VideoCapture(source_text)


parser = argparse.ArgumentParser()
parser.add_argument("source", help="RTSP URL, video file, or webcam index like 0")
args = parser.parse_args()

cap = open_capture(args.source)
n = 0
started_at = time.time()
while n < 100:
    ok, frame = cap.read()
    if not ok:
        break
    n += 1

print(
    {
        "opened": cap.isOpened(),
        "frames": n,
        "fps_observed": n / max(time.time() - started_at, 1e-6),
        "width": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
        "height": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
    }
)
cap.release()
