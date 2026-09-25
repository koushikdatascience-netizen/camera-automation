from __future__ import annotations

import argparse
import json
from pathlib import Path
from statistics import mean
from time import perf_counter

from camera_service.domain import ResourceScope
from camera_service.frame_sources import OpenCVFrameSource
from camera_service.inference import UltralyticsCPUBackend


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark normalized LOCAL_CPU inference on a repeatable video source.")
    parser.add_argument("video", help="Path to a local MP4/video file")
    parser.add_argument("--model", default="yolo11n.pt", help="Ultralytics model path/name")
    parser.add_argument("--frames", type=int, default=100, help="Maximum frames to infer")
    parser.add_argument("--stride", type=int, default=1, help="Infer every Nth decoded frame")
    parser.add_argument("--tracking", action="store_true", help="Use ByteTrack instead of predict")
    parser.add_argument("--imgsz", type=int, default=384)
    parser.add_argument("--conf", type=float, default=0.20)
    args = parser.parse_args()
    if args.frames < 1 or args.stride < 1:
        parser.error("--frames and --stride must be >= 1")

    scope = ResourceScope(
        tenant_id="dev-tenant",
        company_code="DEV",
        shop_id="dev-shop",
        edge_id="dev-edge",
        camera_id="video-1",
    )
    backend = UltralyticsCPUBackend(args.model)
    latencies = []
    decoded = inferred = detections = failures = 0
    started = perf_counter()

    try:
        with OpenCVFrameSource(args.video) as source:
            while inferred < args.frames:
                packet = source.read()
                if packet is None:
                    break
                decoded += 1
                if (decoded - 1) % args.stride:
                    continue
                try:
                    result = backend.infer(
                        packet.frame,
                        scope=scope,
                        frame_id=packet.frame_id,
                        tracking=args.tracking,
                        imgsz=args.imgsz,
                        conf=args.conf,
                    )
                except Exception as exc:
                    failures += 1
                    print(json.dumps({"frame_id": packet.frame_id, "error": type(exc).__name__}))
                    continue
                inferred += 1
                detections += len(result.detections)
                latencies.append(result.inference_latency_ms)
    finally:
        elapsed = max(perf_counter() - started, 1e-9)

    ordered = sorted(latencies)
    def percentile(p: float) -> float:
        if not ordered:
            return 0.0
        return ordered[min(len(ordered) - 1, round((len(ordered) - 1) * p))]

    report = {
        "source": str(Path(args.video)),
        "backend": backend.backend_type.value,
        "model": args.model,
        "tracking": args.tracking,
        "decoded_frames": decoded,
        "inferred_frames": inferred,
        "detections": detections,
        "failures": failures,
        "wall_seconds": round(elapsed, 3),
        "effective_inference_fps": round(inferred / elapsed, 3),
        "latency_ms": {
            "mean": round(mean(latencies), 3) if latencies else 0.0,
            "p50": round(percentile(0.50), 3),
            "p95": round(percentile(0.95), 3),
            "p99": round(percentile(0.99), 3),
        },
        "health": backend.health(),
    }
    print(json.dumps(report, indent=2))
    return 0 if inferred and not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
