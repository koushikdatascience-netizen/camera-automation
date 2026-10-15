from datetime import datetime, timezone

import pytest

from camera_service.domain import (
    BackendType,
    Detection,
    EdgeEvent,
    EventType,
    InferenceResult,
    ResourceScope,
)
from camera_service.inference import InferenceBackendRouter


def scope() -> ResourceScope:
    return ResourceScope(
        tenant_id="tenant-1",
        company_code="2",
        shop_id="WBTEST",
        edge_id="edge-1",
        camera_id="camera-1",
    )


def test_normalized_inference_result_carries_scope_and_backend():
    result = InferenceResult(
        scope=scope(),
        frame_id="frame-1",
        timestamp=datetime.now(timezone.utc),
        model_id="scissors",
        model_version="v3",
        detections=[
            Detection(class_name="scissors", confidence=0.91, bbox_xyxy=(1, 2, 30, 40))
        ],
        inference_latency_ms=18.4,
        backend_type=BackendType.LOCAL_CPU,
    )
    assert result.scope.shop_id == "WBTEST"
    assert result.backend_type is BackendType.LOCAL_CPU
    assert result.schema_version == 1


def test_edge_event_has_stable_id_and_schema_version():
    event = EdgeEvent(
        event_type=EventType.CAMERA_OFFLINE,
        scope=scope(),
        payload={"reason": "timeout"},
    )
    assert event.event_id
    assert event.schema_version == 1


def test_scope_requires_tenant_shop_edge_and_camera():
    with pytest.raises(Exception):
        ResourceScope(tenant_id="tenant-1", shop_id="", edge_id="edge-1", camera_id="camera-1")


def test_router_fails_closed_for_unregistered_backend():
    router = InferenceBackendRouter()
    with pytest.raises(RuntimeError, match="CLOUD_GPU"):
        router.get(BackendType.CLOUD_GPU)
