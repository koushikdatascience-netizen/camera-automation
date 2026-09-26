from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from statistics import mean
from time import perf_counter

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import cv2

from camera_service.camera_manager import CameraConfig, CameraManager


def run(video: str, model: str, frames: int, tracking: bool, use_router: bool) -> dict:
    if use_router:
        os.environ["SNAPKEY_INFERENCE_ROUTER_ENABLED"] = "1"
    else:
        os.environ.pop("SNAPKEY_INFERENCE_ROUTER_ENABLED", None)

    manager = CameraManager(":memory:")
    config = CameraConfig(
        camera_id="parity-camera",
        name="Parity Camera",
        source_type="file",
        rtsp_url=video,
        tracking_mode="track" if tracking else "detect",
    )
    capture = cv2.VideoCapture(video)
    if not capture.isOpened():
        raise RuntimeError(f"Could not open video: {video}")

    state = {}
    latencies = []
    people = []
    failures = 0
    processed = 0
    started = perf_counter()
    try:
        while processed < frames:
            ok, frame = capture.read()
            if not ok or frame is None:
                break
            t0 = perf_counter()
            manager._annotate_tracking_frame(
                frame,
                model,
                camera_config=config,
                stream_state=state,
            )
            latencies.append((perf_counter() - t0) * 1000.0)
            summary = state.get("latest_summary", {})
            if summary.get("error"):
                failures += 1
            people.append(int(summary.get("people", 0) or 0))
            processed += 1
    finally:
        capture.release()
    elapsed = max(perf_counter() - started, 1e-9)
    ordered = sorted(latencies)

    def percentile(p):
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * p))]

    return {
        "path": "normalized" if use_router else "legacy",
        "tracking": tracking,
        "frames": processed,
        "failures": failures,
        "people_total": sum(people),
        "people_frames": sum(1 for count in people if count > 0),
        "effective_fps": round(processed / elapsed, 3),
        "latency_ms": {
            "mean": round(mean(latencies), 3) if latencies else 0.0,
            "p50": round(percentile(.50), 3),
            "p95": round(percentile(.95), 3),
            "p99": round(percentile(.99), 3),
        },
        "reported_backend": state.get("inference_backend", "LEGACY"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Compare legacy and normalized CameraManager inference on identical video.")
    parser.add_argument("video")
    parser.add_argument("--model", default="yolo11n.pt")
    parser.add_argument("--frames", type=int, default=100)
    parser.add_argument("--tracking", action="store_true")
    args = parser.parse_args()

    legacy = run(args.video, args.model, args.frames, args.tracking, False)
    normalized = run(args.video, args.model, args.frames, args.tracking, True)
    people_delta = normalized["people_total"] - legacy["people_total"]
    report = {
        "legacy": legacy,
        "normalized": normalized,
        "comparison": {
            "people_total_delta": people_delta,
            "people_total_equal": people_delta == 0,
            "failure_free": legacy["failures"] == 0 and normalized["failures"] == 0,
            "normalized_vs_legacy_fps_ratio": round(
                normalized["effective_fps"] / max(legacy["effective_fps"], 1e-9), 3
            ),
        },
    }
    print(json.dumps(report, indent=2))
    return 0 if report["comparison"]["failure_free"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
