from fastapi.testclient import TestClient

from camera_service.camera_manager import CameraManager
from cloud_portal.storage import PortalStore


def test_detection_mode_defaults_full_frame_and_empty_custom_is_disabled(tmp_path):
    manager=CameraManager(str(tmp_path/"edge.db"))
    manager.create_camera({"camera_id":"camera-current","name":"Entry","rtsp_url":"0",
        "camera_role":"ENTRANCE_EXIT","features":{"unknown_detection":True}})
    default=manager.get_detection_config("camera-current",True)
    assert default["mode"]=="FULL_FRAME" and default["effective_mode"]=="FULL_FRAME"
    assert default["effective_zones"]==[{"id":"FULL_FRAME","name":"Full frame","x":0.0,"y":0.0,"width":1.0,"height":1.0,"enabled":True}]
    manager.replace_detection_config("camera-current","CUSTOM_ZONES",[],expected_version=0)
    empty=manager.get_detection_config("camera-current",True)
    assert empty["mode"]=="CUSTOM_ZONES" and empty["effective_mode"]=="DISABLED" and empty["effective_zones"]==[]
    assert manager.get_detection_config("camera-current",False)["effective_mode"]=="DISABLED"


def test_current_camera_zones_persist_and_legacy_camera_is_not_reassigned(tmp_path):
    path=tmp_path/"edge.db";manager=CameraManager(str(path))
    for cid in ("webcam_1","camera-current"):
        manager.create_camera({"camera_id":cid,"name":cid,"rtsp_url":"0" if cid=="webcam_1" else "1",
            "camera_role":"ENTRANCE_EXIT","features":{"unknown_detection":True}})
    manager.save_security_zone("camera-current",{"id":"front","name":"Front","x":.1,"y":.2,"width":.4,"height":.5,"enabled":True})
    manager.update_camera_status("camera-current",state="ONLINE",online=True,last_frame_at="2026-10-08T10:00:00+00:00",frame_width=1280,frame_height=720)
    reloaded=CameraManager(str(path))
    assert [z["id"] for z in reloaded.list_security_zones("camera-current")]==["front"]
    assert reloaded.list_security_zones("webcam_1")==[]
    assert (reloaded.get_camera_status("camera-current").frame_width,reloaded.get_camera_status("camera-current").frame_height)==(1280,720)
    assert reloaded._matching_security_zone("camera-current",(20,30,40,50),(100,100,3))["id"]=="front"


def test_cloud_zone_sync_is_versioned_cached_offline_and_respects_local_override(tmp_path):
    manager=CameraManager(str(tmp_path/"edge.db"))
    manager.create_camera({"camera_id":"camera-1","name":"Entry","rtsp_url":"0","camera_role":"ENTRANCE_EXIT","features":{"unknown_detection":True}})
    cfg={"mode":"CUSTOM_ZONES","version":3,"zones":[{"id":"z1","name":"Entrance","x":0,"y":0,"width":.5,"height":1,"enabled":True}]}
    applied=manager.apply_cloud_detection_config("camera-1",cfg)
    assert applied["cloud_version"]==3 and applied["sync_status"]=="SYNCED"
    assert manager._matching_security_zone("camera-1",(20,20,30,30),(100,100,3))["id"]=="z1"
    manager.apply_cloud_detection_config("camera-1",{"mode":"FULL_FRAME","version":4,"zones":[]})
    assert manager.get_detection_config("camera-1")["cloud_version"]==4
    manager.replace_detection_config("camera-1","CUSTOM_ZONES",[{"id":"local","name":"Local","x":0,"y":0,"width":1,"height":1}],expected_version=2)
    result=manager.apply_cloud_detection_config("camera-1",{"mode":"FULL_FRAME","version":5,"zones":[]})
    assert result["local_override"] is True
    assert manager.list_security_zones("camera-1")[0]["id"]=="local"


def test_cloud_and_crm_zone_apis_are_scoped_versioned_and_incidents_filter_camera(tmp_path,monkeypatch):
    monkeypatch.setenv("SNAPKEY_PORTAL_DB",str(tmp_path/"portal.db"));monkeypatch.delenv("SNAPKEY_DATABASE_URL",raising=False)
    monkeypatch.setenv("SNAPKEY_ENV","development");monkeypatch.setenv("SNAPKEY_CRM_INTEGRATION_KEY","zone-crm-key-0123456789")
    import importlib,cloud_portal.api as api
    importlib.reload(api)
    api.store.upsert_camera({"tenant_id":"tenant-a","company_code":"COMP","shop_id":"shop-a","site_id":"site-a","edge_id":"edge-a","camera_id":"camera-1","name":"Entry","source_type":"webcam","source":"edge-local:camera-1","camera_role":"ENTRANCE_EXIT","features":{"unknown_detection":True}})
    client=TestClient(api.app)
    auth=client.post("/crm/session",json={"tenantId":"tenant-a","shopCode":"shop-a","userId":"admin","role":"ADMIN"},headers={"X-CRM-Integration-Key":"zone-crm-key-0123456789"})
    from urllib.parse import urlparse,parse_qs
    portal={"Authorization":"Bearer "+parse_qs(urlparse(auth.json()["launchUrl"]).fragment)["session"][0]}
    crm={"X-CRM-Integration-Key":"zone-crm-key-0123456789"}
    path="/portal/v1/tenants/tenant-a/cameras/camera-1/detection-config"
    wrong=client.get(path,params={"shop_id":"shop-b","edge_id":"edge-a"},headers=portal)
    assert wrong.status_code==403
    got=client.get(path,params={"shop_id":"shop-a","edge_id":"edge-a"},headers=portal)
    assert got.status_code==200 and got.json()["mode"]=="FULL_FRAME"
    body={"mode":"CUSTOM_ZONES","expected_version":0,"zones":[{"id":"door","name":"Door","x":.1,"y":.1,"width":.5,"height":.8,"enabled":True}]}
    saved=client.put(path,params={"shop_id":"shop-a","edge_id":"edge-a"},json=body,headers=portal)
    assert saved.status_code==200 and saved.json()["version"]==1 and saved.json()["sync_status"]=="PENDING"
    stale=client.put(path,params={"shop_id":"shop-a","edge_id":"edge-a"},json=body,headers=portal)
    assert stale.status_code==409
    crm_path="/integration/v1/tenants/tenant-a/shops/shop-a/cameras/camera-1/detection-config"
    crm_config=client.get(crm_path,params={"edge_id":"edge-a"},headers=crm)
    assert crm_config.status_code==200 and crm_config.json()["zones"][0]["id"]=="door"
    create_zone=client.post("/integration/v1/tenants/tenant-a/shops/shop-a/cameras/camera-1/detection-zones",
        params={"edge_id":"edge-a"},headers=crm,json={"id":"lobby","name":"Lobby","x":0,"y":0,"width":.3,"height":1,"enabled":True,"expected_version":1})
    assert create_zone.status_code==200 and len(create_zone.json()["zones"])==2 and create_zone.json()["version"]==2
    update_zone=client.put("/integration/v1/tenants/tenant-a/shops/shop-a/cameras/camera-1/detection-zones/lobby",
        params={"edge_id":"edge-a"},headers=crm,json={"id":"lobby","name":"Lobby adjusted","x":0,"y":0,"width":.4,"height":1,"enabled":False,"expected_version":2})
    assert update_zone.status_code==200 and update_zone.json()["effective_mode"]=="CUSTOM_ZONES"
    delete_zone=client.delete("/integration/v1/tenants/tenant-a/shops/shop-a/cameras/camera-1/detection-zones/lobby",
        params={"edge_id":"edge-a","expected_version":3},headers=crm)
    assert delete_zone.status_code==200 and delete_zone.json()["mode"]=="CUSTOM_ZONES" and [z["id"] for z in delete_zone.json()["zones"]]==["door"]
    delete_last=client.delete("/integration/v1/tenants/tenant-a/shops/shop-a/cameras/camera-1/detection-zones/door",
        params={"edge_id":"edge-a","expected_version":4},headers=crm)
    assert delete_last.status_code==200 and delete_last.json()["effective_mode"]=="DISABLED"
    bad=client.put(crm_path,params={"edge_id":"edge-other"},json=body,headers=crm)
    assert bad.status_code==404
    api.store.record_portal_event({"tenant_id":"tenant-a","shop_id":"shop-a","site_id":"site-a","edge_id":"edge-a","event_id":"unknown-1","event_type":"UNKNOWN_INCIDENT","event_time":"2026-10-08T10:00:00+00:00","camera_id":"camera-1","payload":{"metadata":{"evidence_status":"PARTIAL"}}})
    incidents=client.get("/integration/v1/tenants/tenant-a/shops/shop-a/unknown-incidents",params={"camera_id":"camera-1"},headers=crm)
    assert incidents.status_code==200 and incidents.json()["items"][0]["camera_id"]=="camera-1"


def test_edge_camera_configuration_ack_is_edge_scoped(tmp_path):
    store=PortalStore(str(tmp_path/"portal.db"))
    store.upsert_camera({"tenant_id":"t","shop_id":"s","site_id":"site","edge_id":"e","camera_id":"c","name":"cam","source":"edge-local:c"})
    assert store.acknowledge_detection_config("t","s","e","c",0,"APPLIED")
    config=store.get_detection_config("t","s","e","c")
    assert config["sync_status"]=="APPLIED" and config["applied_version"]==0
