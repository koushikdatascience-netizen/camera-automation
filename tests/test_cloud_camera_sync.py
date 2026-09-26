from types import SimpleNamespace

from camera_service.camera_manager import CameraManager
from camera_service.sync_worker import EdgeSyncWorker


def cloud_camera(camera_id="CAM-1", source="rtsp://user:pass@192.168.1.20/stream"):
    return {
        "tenant_id":"tenant-1","company_code":"2","shop_id":"WBTEST","site_id":"site-1","edge_id":"edge-1",
        "camera_id":camera_id,"name":"Main Entrance","source_type":"rtsp","source":source,
        "camera_role":"ENTRANCE_EXIT","camera_zone":"Main Gate","crowd_threshold":12,"enabled":True,
        "features":{"face_recognition":True,"tracking":True,"object_security":True},
        "settings":{"tracking_fps":4,"max_frame_width":640,"tracking_quality":70,"tracking_mode":"track"},
    }


def test_camera_manager_applies_cloud_assignment_idempotently(tmp_path):
    manager=CameraManager(str(tmp_path/"edge.db"))
    first=manager.apply_cloud_camera(cloud_camera())
    assert first.camera_id=="CAM-1"
    assert first.rtsp_url.startswith("rtsp://")
    assert first.features.face_recognition is True
    assert first.features.object_security is True
    assert first.camera_zone.value=="inside"
    updated=cloud_camera()
    updated["name"]="Front Gate"
    updated["source"]="0"
    updated["source_type"]="webcam"
    second=manager.apply_cloud_camera(updated)
    assert second.name=="Front Gate"
    assert second.source_type=="webcam"
    assert second.rtsp_url=="0"
    assert len(manager.list_cameras())==1


def test_camera_manager_accepts_generic_source_types_without_vendor_lockin(tmp_path):
    manager=CameraManager(str(tmp_path/"edge.db"))
    for camera_id, source_type, source in [
        ("IP-1","rtsp","rtsp://10.0.0.2/live"),
        ("USB-1","webcam","0"),
        ("FILE-1","file","C:/video/test.mp4"),
    ]:
        camera=cloud_camera(camera_id,source)
        camera["source_type"]=source_type
        manager.apply_cloud_camera(camera)
    assert {c.source_type for c in manager.list_cameras()}=={"rtsp","webcam","file"}


class DummyStore:
    def queued_events(self, _limit): return []
    def now(self): return "now"


class DummyLicense:
    def status(self): return SimpleNamespace(active=True, reason="", model_dump=lambda: {})


class DummyCloud:
    def enabled(self): return True
    def camera_config(self): return {"items":[cloud_camera()]}
    def post_event(self, edge, event): return {"ok":True}


def test_sync_worker_applies_cloud_camera_assignments(tmp_path):
    manager=CameraManager(str(tmp_path/"edge.db"))
    worker=EdgeSyncWorker(
        DummyStore(),DummyCloud(),SimpleNamespace(),SimpleNamespace(batch_size=50),
        DummyLicense(),camera_manager=manager,
    )
    result=worker.run_once()
    assert result.failed==0
    assert manager.get_camera("CAM-1") is not None
    assert worker.last_result["camera_sync"]=={"fetched":1,"applied":1,"failed":0}
