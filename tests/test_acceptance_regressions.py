import base64
import hashlib
from types import SimpleNamespace
import cv2
import numpy as np


def test_raw_jpeg_base64_is_not_a_remote_path(monkeypatch):
    from cloud_portal import api
    ok, encoded = cv2.imencode(".jpg", np.zeros((64,64,3), dtype=np.uint8))
    assert ok
    image = base64.b64encode(encoded).decode()
    assert image.startswith("/9j/")
    assert api._decode_crm_face_image(image) == encoded.tobytes()
    assert api._decode_crm_face_image("/faces/photo.jpg") is None
    assert api._decode_crm_face_image("https://crm.test/photo.jpg") is None
    monkeypatch.setattr(api, "_crm_allowed_user_ids", lambda tenant: {"employee"})
    monkeypatch.setattr(api.crm_client, "face_embeddings", lambda tenant, **kwargs: [{"id":"employee", "tenantId":"tenant", "faceImages":[image]}])
    assert api._crm_face_login_identity("tenant", "employee") == ("tenant", image)


def test_actual_evidence_writer_produces_decodable_webm(tmp_path):
    from camera_service.camera_manager import CameraManager
    frame = np.zeros((64,64,3), dtype=np.uint8)
    _, encoded = cv2.imencode(".jpg", frame)
    updates = []
    store = SimpleNamespace(update_person_event_evidence=lambda *args, **kwargs: updates.append(kwargs))
    manager = SimpleNamespace(db_path=str(tmp_path / "edge.db"))
    CameraManager._write_security_clip(manager, {"event_kind":"attendance", "alert_id":"test", "camera_id":"camera"}, [encoded.tobytes()]*9, store)
    assert len(updates) == 1
    clip = updates[0]["clip_path"]
    assert clip.endswith(".webm")
    reader = cv2.VideoCapture(clip)
    try:
        assert reader.isOpened() and reader.read()[0]
    finally:
        reader.release()


def test_manual_evidence_upload_uses_portable_collision_safe_name(tmp_path, monkeypatch):
    import asyncio
    from io import BytesIO
    from starlette.datastructures import UploadFile, Headers
    from cloud_portal import api
    monkeypatch.setenv("SNAPKEY_EVIDENCE_ROOT", str(tmp_path))
    principal = SimpleNamespace(tenant_id="tenant", shop_id="shop", edge_id="edge", legacy_global=False)
    outputs = []
    for event_id in ("manual:123:snapshot-1", "manual_123_snapshot-1", hashlib.sha256(b"manual:123:snapshot-1").hexdigest()):
        upload = UploadFile(BytesIO(b"synthetic"), filename="test.webm", headers=Headers({"content-type":"video/webm"}))
        outputs.append(asyncio.run(api.upload_edge_event_evidence(event_id, upload, principal)))
    assert outputs[0]["event_id"] == "manual:123:snapshot-1"
    assert ":" not in outputs[0]["evidence_id"]
    assert len({item["evidence_id"] for item in outputs}) == 3
    assert (tmp_path / outputs[0]["evidence_id"]).read_bytes() == b"synthetic"
