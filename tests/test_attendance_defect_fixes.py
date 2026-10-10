from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from camera_service.storage import SQLiteStore
from cloud_portal import api
from cloud_portal.api import PortalPrincipal


def test_manual_successful_checkin_updates_presence_for_absence_monitoring(monkeypatch):
    calls=[]
    now=datetime.now(timezone.utc)
    event={"id":"recognition-1","event_type":"PERSON_RECOGNIZED","camera_id":"cam",
           "edge_id":"edge","event_time":now.isoformat(),
           "payload":{"payload":{"person_id":"person","metadata":{"snapshot_paths":[]}}}}

    class Store:
        def get_event(self,*_args): return event
        def crm_person_mapping(self,*_args): return {"crm_user_id":"crm-user"}
        def set_attendance_presence_break(self,*args): calls.append(("break",args[-1]))
        def touch_attendance_presence(self,**kwargs): calls.append(("presence",kwargs))
        def record_attendance_activity(self,item): calls.append(("activity",item))
        def record_portal_event(self,item): calls.append(("event",item))

    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_portal_scope",lambda *_args:None)
    monkeypatch.setattr(api,"_portal_camera_lookup",lambda *_args:{"camera_role":"ENTRANCE_EXIT","camera_zone":"inside"})
    monkeypatch.setattr(api,"_crm_tenant_uuid_for_user",lambda *_args:"crm-tenant")
    monkeypatch.setattr(api,"_crm_face_token",lambda *_args,**_kwargs:"temporary")
    monkeypatch.setattr(api,"_crm_face_login_succeeded",lambda _result:True)
    monkeypatch.setattr(api,"_crm_mutation_succeeded",lambda _result:True)
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(face_attendance_configured=True,
        login_using_face_tenant=lambda *_args:{"token":"temporary","user":{"id":"crm-user"}},
        login_logout_with_face_token=lambda *_args:{"success":True}))

    result=api.attendance_station_action("tenant",api.AttendanceStationActionRequest(
        camera_id="cam",edge_id="edge",recognition_event_id="recognition-1",action="CHECK_IN"),
        PortalPrincipal("session","tenant",None,"shop","admin","Admin","OWNER"))

    presence=next(item[1] for item in calls if item[0]=="presence")
    assert result["ok"] is True
    assert presence["checked_in"] is True
    assert presence["camera_zone"]=="inside"
    assert any(item[0]=="activity" and item[1]["activity_type"]=="CHECK_IN" for item in calls)


def test_manual_mode_keeps_absence_monitoring_active(monkeypatch):
    now=datetime.now(timezone.utc)
    row={"tenant_id":"tenant","shop_id":"shop","crm_user_id":"user","local_person_id":"person",
         "checked_in":True,"on_break":False,"last_seen_at":now-timedelta(minutes=91),
         "last_camera_id":"cam","last_camera_zone":"inside",
         "policy_json":{"attendanceMode":"MANUAL","absenceMonitoringEnabled":True,
                        "outOfCameraGraceMinutes":5,"adminNotificationAfterMinutes":15,
                        "markAbsentAfterMinutes":60,"timezone":"UTC"}}
    transitions=[];saved=[];coverage=[];logout=[];notifications=[]
    class Store:
        def list_v2_attendance_presence(self,**_kwargs): return [row]
        def attendance_camera_coverage_status(self,*_args,**_kwargs):
            return {"state":"HEALTHY","reason":"healthy","cameraZone":"inside","healthyCameraIds":["cam"]}
        def save_v2_camera_coverage(self,*args): coverage.append(args)
        def count_v2_absence_episodes(self,*_args): return 0
        def record_v2_absence_transition(self,**kwargs): transitions.append(kwargs["transition"]); return True

    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_notify_cloud_event",lambda event:notifications.append(event))
    monkeypatch.setattr(api,"_v2_auto_logout",lambda *_args:logout.append(True))
    api._evaluate_v2_person_absences()

    assert "ADMIN_ABSENCE_WARNING" in transitions
    assert coverage
    assert notifications
    assert logout==[True]


def test_absence_monitoring_can_be_explicitly_disabled_per_person(monkeypatch):
    row={"tenant_id":"tenant","shop_id":"shop","crm_user_id":"user","checked_in":True,
         "last_seen_at":datetime.now(timezone.utc)-timedelta(hours=2),
         "policy_json":{"attendanceMode":"MANUAL","absenceMonitoringEnabled":False}}
    class Store:
        def list_v2_attendance_presence(self,**_kwargs): return [row]
    monkeypatch.setattr(api,"store",Store())
    api._evaluate_v2_person_absences()


def test_person_absence_threshold_and_crm_sixty_minute_floor_both_apply(monkeypatch):
    calls=[]
    now=datetime.now(timezone.utc)
    row={"tenant_id":"tenant","shop_id":"shop","crm_user_id":"user","local_person_id":"person",
         "checked_in":True,"on_break":False,"last_seen_at":now-timedelta(minutes=59),
         "last_camera_id":"cam","last_camera_zone":"inside",
         "policy_json":{"attendanceMode":"AUTO","absenceMonitoringEnabled":True,
                        "outOfCameraGraceMinutes":5,"adminNotificationAfterMinutes":15,
                        "markAbsentAfterMinutes":90,"timezone":"UTC"}}
    class Store:
        def list_v2_attendance_presence(self,**_kwargs): return [row]
        def attendance_camera_coverage_status(self,*_args,**_kwargs): return {"state":"HEALTHY","cameraZone":"inside"}
        def save_v2_camera_coverage(self,*_args): pass
        def count_v2_absence_episodes(self,*_args): return 0
        def record_v2_absence_transition(self,**_kwargs): return True

    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_v2_auto_logout",lambda *_args:calls.append("logout"))
    api._evaluate_v2_person_absences()
    assert calls==[]  # 59 minutes: strictly before CRM contract cutoff
    row["last_seen_at"]=now-timedelta(minutes=61)
    api._evaluate_v2_person_absences()
    assert calls==[]  # person's 90-minute absence threshold is not yet met
    row["last_seen_at"]=now-timedelta(minutes=91)
    api._evaluate_v2_person_absences()
    assert calls==["logout"]

    row["policy_json"]["markAbsentAfterMinutes"]=30
    row["last_seen_at"]=now-timedelta(minutes=59)
    api._evaluate_v2_person_absences()
    assert calls==["logout"]  # CRM's fixed 60-minute contract is still the floor
    row["last_seen_at"]=now-timedelta(minutes=61)
    api._evaluate_v2_person_absences()
    assert calls==["logout","logout"]


def test_camera_outage_persists_unknown_and_suppresses_absence_actions(monkeypatch):
    now=datetime.now(timezone.utc)
    row={"tenant_id":"tenant","shop_id":"shop","crm_user_id":"user","local_person_id":"person",
         "checked_in":True,"on_break":False,"last_seen_at":now-timedelta(hours=2),
         "last_camera_id":"cam","last_camera_zone":"inside",
         "policy_json":{"attendanceMode":"AUTO","absenceMonitoringEnabled":True,
                        "outOfCameraGraceMinutes":5,"adminNotificationAfterMinutes":15,
                        "markAbsentAfterMinutes":60,"timezone":"UTC"}}
    saved=[];transitions=[];logout=[];notify=[]
    class Store:
        def list_v2_attendance_presence(self,**_kwargs): return [row]
        def attendance_camera_coverage_status(self,*_args,**_kwargs):
            return {"state":"UNKNOWN","reason":"heartbeat_stale_or_missing","cameraZone":"inside","healthyCameraIds":[]}
        def save_v2_camera_coverage(self,*args): saved.append(args)
        def count_v2_absence_episodes(self,*_args): return 0
        def record_v2_absence_transition(self,**kwargs): transitions.append(kwargs["transition"]);return True

    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_v2_auto_logout",lambda *_args:logout.append(True))
    monkeypatch.setattr(api,"_notify_cloud_event",lambda event:notify.append(event))
    api._evaluate_v2_person_absences()

    assert saved and saved[0][3]["state"]=="UNKNOWN"
    assert transitions==["CAMERA_COVERAGE_UNKNOWN"]
    assert logout==[] and notify==[]


def test_same_day_reentry_is_allowed_after_presence_was_checked_out(monkeypatch):
    calls=[]
    monkeypatch.setenv("SNAPKEY_CRM_AUTO_LOGIN_ENABLED","1")
    now=datetime.now(timezone.utc)
    class Store:
        def crm_person_mapping(self,*_args): return {"crm_user_id":"crm-user"}
        def touch_attendance_presence(self,**kwargs):
            calls.append(("touch",kwargs))
            return {"checked_in":False}  # prior same-day session is closed
        def person_attendance_policy(self,*_args): return {"attendanceMode":"AUTO"}
        def record_attendance_activity(self,item): calls.append(("activity",item))
        def record_portal_event(self,item): calls.append(("event",item))

    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_portal_camera_lookup",lambda *_args:{"camera_role":"ENTRANCE_EXIT","camera_zone":"inside"})
    monkeypatch.setattr(api,"_crm_tenant_uuid_for_user",lambda *_args:"crm-tenant")
    monkeypatch.setattr(api,"_crm_face_token",lambda *_args:"temporary")
    monkeypatch.setattr(api,"_crm_face_login_succeeded",lambda _result:True)
    monkeypatch.setattr(api,"_crm_mutation_succeeded",lambda _result:True)
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(face_attendance_configured=True,
        login_using_face_tenant=lambda *_args:{"token":"temporary","user":{"id":"crm-user"}},
        login_logout_with_face_token=lambda payload,token:calls.append(("crm-login",payload)) or {"success":True}))
    api._auto_attend_recognized_person({"event_id":"recognition-new","event_type":"PERSON_RECOGNIZED",
        "tenant_id":"tenant","shop_id":"shop","edge_id":"edge","camera_id":"cam",
        "event_time":now.isoformat(),"payload":{"person_id":"person","metadata":{}}})

    assert any(call[0]=="crm-login" for call in calls)
    assert any(call[0]=="activity" and call[1]["activity_type"]=="CHECK_IN" for call in calls)


def test_active_session_still_suppresses_duplicate_recognition(monkeypatch):
    calls=[]
    now=datetime.now(timezone.utc)
    class Store:
        def crm_person_mapping(self,*_args): return {"crm_user_id":"crm-user"}
        def touch_attendance_presence(self,**_kwargs): return {"checked_in":True}
        def person_attendance_policy(self,*_args): return {"attendanceMode":"AUTO"}

    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_portal_camera_lookup",lambda *_args:{"camera_role":"ENTRANCE_EXIT"})
    monkeypatch.setattr(api,"_crm_face_login_identity",lambda *_args:calls.append("face-login") or ("tenant","image"))
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(face_attendance_configured=True))
    api._auto_attend_recognized_person({"event_id":"recognition-dup","event_type":"PERSON_RECOGNIZED",
        "tenant_id":"tenant","shop_id":"shop","edge_id":"edge","camera_id":"cam",
        "event_time":now.isoformat(),"payload":{"person_id":"person","metadata":{}}})
    assert calls==[]


def test_notification_outbox_dispatch_retries_failed_delivery(monkeypatch):
    calls=[]
    class Store:
        def claim_due_notification_deliveries(self,*_args,**_kwargs):
            return [{"id":"delivery-1","attempts":2,"channel":"email","recipient":"ops@example.test",
                     "payload":{"subject":"Alert","body":"Missing person"}}]
        def complete_notification_delivery(self,*_args,**kwargs): calls.append(kwargs)

    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"notification_service",SimpleNamespace(send_email=lambda *_args:
        SimpleNamespace(delivered=False,detail="SMTPError")))
    api._dispatch_notification_outbox()

    assert calls[0]["success"] is False
    assert calls[0]["error"]=="SMTPError"
    assert calls[0]["attempts"]==2
    assert calls[0]["retry_after_seconds"]>0


def test_cloud_alerts_are_deduplicated_into_outbox_before_delivery(monkeypatch):
    queued={}
    class Store:
        def attendance_policy(self,*_args):
            return {"email_recipients":["ops@example.test"],"whatsapp_recipients":[]}
        def person_attendance_policy(self,*_args):
            return {"emailNotificationsEnabled":True,"whatsappNotificationsEnabled":False}
        def enqueue_notification_delivery(self,tenant,shop,event,channel,recipient,payload):
            key=(tenant,shop,event,channel,recipient)
            inserted=key not in queued
            queued[key]=payload
            return inserted

    monkeypatch.setattr(api,"store",Store())
    event={"event_id":"alert-stable","tenant_id":"tenant","shop_id":"shop",
           "event_type":"ATTENDANCE_POLICY_VIOLATION","event_time":"now",
           "payload":{"metadata":{"crm_user_id":"crm-user","reason_code":"ADMIN_ABSENCE_WARNING"}}}
    api._notify_cloud_event(event)
    api._notify_cloud_event(event)

    assert len(queued)==1
    item=next(iter(queued.values()))
    assert item["subject"]=="Camera Eye - ATTENDANCE_POLICY_VIOLATION"
    assert "ADMIN_ABSENCE_WARNING" in item["body"]


def test_confirmed_crm_logout_recovers_local_commit_without_resubmission(monkeypatch):
    now=datetime.now(timezone.utc)
    started=now-timedelta(hours=1)
    row={"tenant_id":"tenant","shop_id":"shop","crm_user_id":"user","local_person_id":"person",
         "checked_in":True,"on_break":False,"last_seen_at":started,"last_camera_id":"cam",
         "last_camera_zone":"inside","policy_json":{"attendanceMode":"AUTO"}}
    calls=[];recoveries=[]
    class Store:
        def attendance_camera_coverage_status(self,*_args,**_kwargs): return {"state":"HEALTHY"}
        def claim_crm_auto_logout(self,*_args): return True
        def mark_crm_auto_logout_confirmed(self,*_args): calls.append("confirmed");return True
        def finalize_crm_auto_logout_local(self,*_args):
            calls.append("finalize")
            if calls.count("finalize")==1:
                raise RuntimeError("temporary database outage")
            return "SUCCEEDED"
        def list_v2_crm_auto_logout_recovery(self,**_kwargs):
            return [{"tenant_id":"tenant","shop_id":"shop","crm_user_id":"user","absence_started_at":started}]

    monkeypatch.setenv("CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED","true")
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_crm_face_token",lambda *_args:"mock-face-token")
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(auto_logout_with_face_token=lambda *_args:
        calls.append("crm") or {"success":True}))
    monkeypatch.setattr(api,"_crm_mutation_succeeded",lambda _result:True)
    api._v2_auto_logout(row,now)
    assert calls==["crm","confirmed","finalize"]

    api._recover_v2_auto_logout_local_finalizations(now+timedelta(seconds=1))
    assert calls==["crm","confirmed","finalize","finalize"]


def test_scoped_reconciliation_endpoint_records_operator_verified_crm_state(monkeypatch):
    now=datetime.now(timezone.utc)-timedelta(hours=1)
    action_id=api._v2_auto_logout_action_id("tenant","shop","user",now)
    calls=[]
    class Store:
        def list_v2_crm_auto_logout_actions(self,*_args):
            return [{"absence_started_at":now,"status":"RECONCILIATION_REQUIRED",
                     "camera_id":"cam","last_recognition_event_id":"recognition-1"}]
        def reconcile_crm_auto_logout_action(self,*args): calls.append(("reconcile",args[-1]));return True
        def finalize_crm_auto_logout_local(self,*args): calls.append(("finalize",args[-1]));return "SUCCEEDED"
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_require_crm_integration",lambda *_args:None)
    result=api.integration_reconcile_person_auto_logout("tenant","shop","user",action_id,
        api.AttendanceLogoutReconciliationRequest(outcome="CRM_CONFIRMED"),SimpleNamespace())
    assert result=={"actionId":action_id,"status":"SUCCEEDED"}
    assert calls[0]==("reconcile","CRM_CONFIRMED")
    assert calls[1][0]=="finalize"
    assert calls[1][1]["camera_id"]=="cam"


def test_edge_sync_uploads_three_action_snapshots_and_clip_with_missing_status(tmp_path):
    from camera_service.sync_worker import EdgeSyncWorker
    paths=[]
    for name in ("one.jpg","two.jpg","three.jpg","attendance.mp4"):
        path=tmp_path/name;path.write_bytes(b"evidence");paths.append(path)
    uploaded=[];posted=[]
    class Store:
        def queued_events(self,_limit):
            event={"event_id":"recognition-1","event_type":"PERSON_RECOGNIZED","camera_id":"cam",
                "metadata":{"face_path":str(paths[0]),"snapshot_path":str(paths[0]),
                            "snapshot_paths":[str(path) for path in paths[:3]],
                            "clip_path":str(paths[3]),"evidence_pending":False}}
            return [{"id":"recognition-1","payload_json":json.dumps(event),"attempts":0}]
        def mark_event_synced(self,_event,*_args): pass
        def now(self): return datetime.now(timezone.utc).isoformat()
    class Cloud:
        def enabled(self): return True
        def heartbeat(self,*_args): return {}
        def edge_commands(self): return []
        def camera_config(self): return {"items":[]}
        def personnel_config(self): return {"items":[]}
        def upload_event_evidence(self,event_id,path):
            uploaded.append((event_id,path));return {"evidence_id":event_id}
        def post_event(self,_edge,event): posted.append(event)
    worker=EdgeSyncWorker(Store(),Cloud(),SimpleNamespace(),SimpleNamespace(batch_size=10),
        SimpleNamespace(status=lambda:SimpleNamespace(active=True,allows_feature=lambda _x:True,model_dump=lambda:{})))
    assert worker.run_once().synced==1
    metadata=posted[0]["metadata"]
    assert len(metadata["cloud_evidence_snapshots"])==3
    assert metadata["cloud_clip"]["evidence_id"]=="recognition-1:attendance-video"
    assert metadata["evidence_status"]=="COMPLETE"
    assert "snapshot_paths" not in metadata and "clip_path" not in metadata
    assert [entry["index"] for entry in metadata["cloud_evidence_snapshots"]]==[0,1,2]


def test_edge_sync_marks_missing_action_evidence_explicitly(tmp_path):
    from camera_service.sync_worker import EdgeSyncWorker
    photo=tmp_path/"one.jpg";photo.write_bytes(b"evidence")
    posted=[]
    class Store:
        def queued_events(self,_limit):
            return [{"id":"recognition-2","payload_json":json.dumps({"event_id":"recognition-2",
                "event_type":"PERSON_RECOGNIZED","metadata":{"snapshot_path":str(photo),
                "snapshot_paths":[str(photo)],"evidence_pending":False}}),"attempts":0}]
        def mark_event_synced(self,_event,*_args): pass
        def now(self): return datetime.now(timezone.utc).isoformat()
    class Cloud:
        def enabled(self): return True
        def heartbeat(self,*_args): return {}
        def edge_commands(self): return []
        def camera_config(self): return {"items":[]}
        def personnel_config(self): return {"items":[]}
        def upload_event_evidence(self,event_id,_path): return {"evidence_id":event_id}
        def post_event(self,_edge,event): posted.append(event)
    worker=EdgeSyncWorker(Store(),Cloud(),SimpleNamespace(),SimpleNamespace(batch_size=10),
        SimpleNamespace(status=lambda:SimpleNamespace(active=True,allows_feature=lambda _x:True,model_dump=lambda:{})))
    assert worker.run_once().synced==1
    metadata=posted[0]["metadata"]
    assert metadata["evidence_status"]=="PARTIAL"
    assert metadata["evidence_missing"]["snapshots"]=="expected_3_received_1"
    assert metadata["evidence_missing"]["clip"]=="not_captured"


def test_edge_evidence_completion_is_durable_and_explicit(tmp_path):
    store=SQLiteStore(str(tmp_path/"edge.db"))
    event_id=store.add_person_event("person","shop","cam","PERSON_RECOGNIZED",
                                    datetime.now(timezone.utc),{"evidence_pending":True})
    assert store.update_person_event_evidence(event_id,snapshot_paths=["one.jpg","two.jpg"],
        evidence_missing={"snapshots":"capture_incomplete_2_of_3","clip":"camera_clip_capture_busy"},
        evidence_pending=False)
    row=next(item for item in store.queued_events(20) if item["id"]==event_id)
    metadata=json.loads(row["payload_json"])["metadata"]
    assert metadata["evidence_status"]=="PARTIAL"
    assert metadata["evidence_missing"]["clip"]=="camera_clip_capture_busy"
    assert metadata["snapshot_paths"]==["one.jpg","two.jpg"]


def test_edge_restart_releases_stale_evidence_queue_with_missing_reason(tmp_path):
    path=tmp_path/"edge.db"
    store=SQLiteStore(str(path))
    event_id=store.add_person_event("person","shop","cam","PERSON_RECOGNIZED",
        datetime.now(timezone.utc)-timedelta(minutes=2),{"snapshot_paths":["one.jpg"],"evidence_pending":True})
    with store._conn() as conn:
        conn.execute("UPDATE edge_event_queue SET created_at=? WHERE id=?",
                     ((datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat(),event_id))
    restarted=SQLiteStore(str(path))
    row=next(item for item in restarted.queued_events(10) if item["id"]==event_id)
    metadata=json.loads(row["payload_json"])["metadata"]
    assert metadata["evidence_pending"] is False
    assert metadata["evidence_missing"]["clip"]=="capture_interrupted_by_edge_restart"
    assert metadata["evidence_status"]=="PARTIAL"


def test_camera_manager_captures_three_attendance_snapshots_and_short_clip(monkeypatch):
    from collections import deque
    from camera_service.camera_manager import CameraManager
    manager=CameraManager.__new__(CameraManager)
    stream={"evidence_buffer":deque([b"old-frame"]*10,maxlen=30),"security_clip":None}
    assert manager._begin_evidence_clip(stream,"action-1","cam","attendance",post_duration=3,prebuffer_count=6)
    assert stream["security_clip"]["post_duration"]==3
    assert len(stream["security_clip"]["prebuffer"])==6
    saved=[];updated=[]
    import camera_service.camera_manager as camera_manager_module
    clock=SimpleNamespace(value=0.0,monotonic=lambda:clock.value)
    monkeypatch.setattr(camera_manager_module,"time",clock)
    task={"event_id":"action-2","camera_id":"cam","started_at":0.0,"last_snapshot_at":0.0,
          "snapshot_paths":["snap-1.jpg"],"clip_started":False,"clip_missing_reason":"camera_clip_capture_busy"}
    evidence_state={"attendance_evidence_tasks":{"action-2":task},"security_clip":None}
    for stamp in (0.6,1.2,3.2):
        clock.value=stamp
        manager._save_event_snapshot=lambda _frame,_camera,_prefix:f"snap-{len(saved)+2}.jpg"
        manager._record_attendance_evidence_frame(evidence_state,b"frame",SimpleNamespace(
            update_person_event_evidence=lambda event_id,**kwargs:updated.append((event_id,kwargs))))
        saved.append(stamp)
    assert updated[0][0]=="action-2"
    assert len(updated[0][1]["snapshot_paths"])==3
    assert updated[0][1]["evidence_pending"] is False
    assert updated[0][1]["evidence_missing"]["clip"]=="camera_clip_capture_busy"


def test_action_evidence_manifest_exposes_three_assets_and_missing_reasons():
    complete=api._action_evidence_manifest({"cloud_evidence_snapshots":[
        {"index":i,"evidence":{"evidence_id":f"asset-{i}"}} for i in range(3)],
        "cloud_clip":{"evidence_id":"clip-1"}},"recognition-1")
    assert complete["status"]=="COMPLETE"
    assert complete["recognitionEventId"]=="recognition-1"
    partial=api._action_evidence_manifest({"cloud_evidence_snapshots":[
        {"index":0,"evidence":{"evidence_id":"asset-0"}}],
        "evidence_missing":{"clip":"writer_unavailable"}},"recognition-2")
    assert partial["status"]=="PARTIAL"
    assert partial["missing"]["clip"]=="writer_unavailable"
    assert partial["missing"]["snapshots"]=="expected_3_received_1"


def test_action_evidence_proxy_resolves_only_the_linked_scoped_media(tmp_path,monkeypatch):
    from fastapi import HTTPException
    root=tmp_path/"evidence";allowed=root/"tenant"/"shop"/"edge";allowed.mkdir(parents=True)
    media=[]
    for index in range(3):
        path=allowed/f"snap-{index}.jpg";path.write_bytes(f"image-{index}".encode());media.append(path)
    clip=allowed/"clip.mp4";clip.write_bytes(b"video")
    event={"edge_id":"edge","payload":{"payload":{"metadata":{
        "cloud_evidence_snapshots":[{"index":index,"evidence":{"evidence_id":f"tenant/shop/edge/snap-{index}.jpg"}}
                                    for index in range(3)],
        "cloud_clip":{"evidence_id":"tenant/shop/edge/clip.mp4"}}}}}
    class Store:
        def get_event(self,*_args): return event
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setenv("SNAPKEY_EVIDENCE_ROOT",str(root))

    assert api._resolve_attendance_event_media("tenant","shop","recognition-1","snapshot",2)==media[2]
    assert api._resolve_attendance_event_media("tenant","shop","recognition-1","video")==clip
    event["payload"]["payload"]["metadata"]["cloud_evidence_snapshots"][2]["evidence"]["evidence_id"]="tenant/shop/edge/../../outside.jpg"
    with pytest.raises(HTTPException) as error:
        api._resolve_attendance_event_media("tenant","shop","recognition-1","snapshot",2)
    assert error.value.status_code==404


def test_legacy_automatic_checkout_scheduler_never_claims_when_disabled(monkeypatch):
    calls=[]
    class Store:
        def claim_due_absence_checkouts(self,*_args,**_kwargs):
            calls.append("absence-claim"); return []
        def claim_due_max_logoff_checkouts(self,*_args,**_kwargs):
            calls.append("max-logoff-claim"); return []

    monkeypatch.delenv("SNAPKEY_CRM_AUTO_LOGOUT_ENABLED",raising=False)
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_evaluate_v2_person_absences",lambda:calls.append("v2-monitor"))
    monkeypatch.setattr(api,"_dispatch_notification_outbox",lambda **_kwargs:calls.append("notifications"))

    api._evaluate_absence_checkouts()

    assert calls==["v2-monitor","notifications"]


def test_legacy_automatic_checkout_defense_in_depth_releases_claim_without_crm(monkeypatch):
    completions=[];crm_calls=[]
    class Store:
        def complete_presence_checkout(self,*args): completions.append(args)

    monkeypatch.setenv("SNAPKEY_CRM_AUTO_LOGOUT_ENABLED","0")
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(login_logout=lambda payload:crm_calls.append(payload)))
    presence={"tenant_id":"tenant","shop_id":"shop","local_person_id":"person",
              "crm_user_id":"crm-user","last_seen_at":datetime.now(timezone.utc),
              "last_camera_id":"camera"}

    api._process_automatic_checkout(presence,now=datetime.now(timezone.utc),
                                    reason_code="ABSENCE_GRACE_EXCEEDED",
                                    require_camera_health=True)

    assert completions==[("tenant","shop","person",False)]
    assert crm_calls==[]


def test_v2_automatic_checkout_flag_off_blocks_crm_mutation(monkeypatch):
    from cloud_portal import api

    calls=[]
    row={"tenant_id":"tenant","shop_id":"shop","crm_user_id":"user",
         "local_person_id":"person","checked_in":True,"on_break":False,
         "last_seen_at":datetime.now(timezone.utc)-timedelta(hours=2),
         "last_camera_id":"cam","last_camera_zone":"inside",
         "policy_json":{"absenceMonitoringEnabled":True}}
    monkeypatch.setenv("CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED","false")
    monkeypatch.setattr(api,"store",SimpleNamespace(
        attendance_camera_coverage_status=lambda *_args,**_kwargs:{"state":"HEALTHY"},
        claim_crm_auto_logout=lambda *_args:calls.append("claim") or True))
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(
        auto_logout_with_face_token=lambda *_args:calls.append("crm")))

    api._v2_auto_logout(row,datetime.now(timezone.utc))

    assert calls==[]


def test_legacy_max_logoff_policy_still_runs_when_auto_logout_enabled(monkeypatch):
    from cloud_portal import api

    calls=[]
    due={"tenant_id":"tenant","shop_id":"shop","local_person_id":"person",
         "crm_user_id":"crm-user","last_seen_at":datetime.now(timezone.utc),
         "last_camera_id":"camera"}
    class Store:
        def claim_due_max_logoff_checkouts(self,*_args,**_kwargs): return [due]
        def claim_due_absence_checkouts(self,*_args,**_kwargs): return []

    monkeypatch.setenv("SNAPKEY_CRM_AUTO_LOGOUT_ENABLED","1")
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_evaluate_v2_person_absences",lambda:None)
    monkeypatch.setattr(api,"_process_automatic_checkout",lambda presence,**kwargs:
                        calls.append((presence,kwargs)))
    monkeypatch.setattr(api,"_dispatch_notification_outbox",lambda **_kwargs:None)

    api._evaluate_absence_checkouts()

    assert len(calls)==1
    assert calls[0][0] is due
    assert calls[0][1]["reason_code"]=="MAX_LOGOFF_REACHED"
    assert calls[0][1]["require_camera_health"] is False


@pytest.mark.parametrize("directory,roster,should_raise",[
    ({"items":[{"id":"employee-1"}]},[{"userId":"employee-1"}],False),
    ({"data":[{"id":"tenant-a-user"}]},[{"userId":"tenant-b-user"}],True),
    ([{"id":1395}],[{"userId":"1395"}],False),
])
def test_crm_service_token_scope_uses_only_matching_crm_user_ids(
    monkeypatch,directory,roster,should_raise
):
    from types import SimpleNamespace
    from cloud_portal import api

    monkeypatch.setattr(api,"crm_client",SimpleNamespace(
        face_embeddings=lambda _tenant, **kwargs:directory,
        users_roster=lambda *_args,**_kwargs:roster))

    if should_raise:
        with pytest.raises(RuntimeError,match="scoped to a different tenant"):
            api._assert_crm_service_token_scope("tenant-code")
    else:
        api._assert_crm_service_token_scope("tenant-code")


def test_crm_service_token_scope_fails_closed_for_empty_or_unrecognized_roster(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(api,"crm_client",SimpleNamespace(
        face_embeddings=lambda _tenant, **kwargs:{"items":[{"id":"employee-1"}]},
        users_roster=lambda *_args,**_kwargs:{"data":[{"userId":"employee-1"}]}))

    with pytest.raises(RuntimeError,match="scope could not be verified"):
        api._assert_crm_service_token_scope("tenant-code")


def test_crm_service_token_scope_rejects_user_outside_verified_tenant_intersection(monkeypatch):
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(
        face_embeddings=lambda _tenant, **kwargs:[{"id":"employee-a"},{"id":"employee-b"}],
        users_roster=lambda *_args,**_kwargs:[{"userId":"employee-a"},{"userId":"employee-b"}]))

    with pytest.raises(RuntimeError,match="does not include the requested CRM user"):
        api._assert_crm_service_token_scope("tenant-code","employee-c")


def test_legacy_checkout_uses_verified_employee_face_token(monkeypatch):
    calls=[]
    class Store:
        def claim_crm_auto_logout(self,*args,**kwargs): return True
        def mark_crm_auto_logout_confirmed(self,*args): return True
        def finalize_crm_auto_logout_local(self,*args):
            calls.append(("complete",("tenant","shop","person",True))); return "SUCCEEDED"
        def attendance_policy(self,*args): return {"timezone":"Asia/Kolkata"}
        def attendance_camera_coverage_healthy(self,*_args,**_kwargs):
            raise AssertionError("max-logoff keeps its existing coverage-independent policy")
        def complete_presence_checkout(self,*args): calls.append(("complete",args))
        def record_attendance_activity(self,item): calls.append(("activity",item))
        def record_portal_event(self,item): calls.append(("event",item))

    monkeypatch.setenv("SNAPKEY_CRM_AUTO_LOGOUT_ENABLED","1")
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_crm_face_token",
                        lambda tenant,shop,user:"employee-face-token")
    monkeypatch.setattr(api,"_evidence_manifest_for_last_recognition",lambda *_args:{"status":"UNAVAILABLE"})
    monkeypatch.setattr(api,"_notify_cloud_event",lambda _event:None)
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(
        login_logout=lambda *_args:pytest.fail("static service credential must not perform attendance logout"),
        login_logout_with_face_token=lambda payload,token:
            calls.append(("crm",payload,token)) or {"success":True}))
    monkeypatch.setattr(api,"_crm_mutation_succeeded",lambda _result:True)
    now=datetime.now(timezone.utc)
    presence={"tenant_id":"tenant","shop_id":"shop","local_person_id":"person",
              "crm_user_id":"employee-a","last_seen_at":now-timedelta(hours=1),
              "last_camera_id":"camera"}

    api._process_automatic_checkout(presence,now=now,reason_code="MAX_LOGOFF_REACHED",
                                    require_camera_health=False)

    crm_call=next(call for call in calls if call[0]=="crm")
    assert crm_call[1]["userId"]=="employee-a"
    assert crm_call[2]=="employee-face-token"
    assert ("complete",("tenant","shop","person",True)) in calls


def test_v2_auto_logout_invalidates_token_on_401_without_retry(monkeypatch):
    import httpx

    now=datetime.now(timezone.utc);started=now-timedelta(minutes=70);calls=[]
    class Store:
        def attendance_camera_coverage_status(self,*_args,**_kwargs): return {"state":"HEALTHY"}
        def claim_crm_auto_logout(self,*_args): return True
        def delete_crm_face_token(self,*args): calls.append(("invalidate",args))
        def mark_crm_auto_logout_reconciliation_required(self,*args): calls.append(("reconcile",args))

    response=httpx.Response(401,request=httpx.Request("POST","https://crm.invalid"))
    error=httpx.HTTPStatusError("unauthorized",request=response.request,response=response)
    def rejected(*_args):
        calls.append(("crm",))
        raise error
    monkeypatch.setenv("CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED","true")
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_crm_face_token",lambda *_args:"revoked-face-token")
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(auto_logout_with_face_token=rejected))
    row={"tenant_id":"tenant","shop_id":"shop","crm_user_id":"employee-a",
         "local_person_id":"person","checked_in":True,"on_break":False,
         "last_seen_at":started,"last_camera_id":"camera","last_camera_zone":"inside",
         "policy_json":{"absenceMonitoringEnabled":True,"markAbsentAfterMinutes":60}}

    api._v2_auto_logout(row,now)

    assert calls[0]==("crm",)
    assert calls[1]==("invalidate",("tenant","shop","employee-a"))
    assert calls[2][0]=="reconcile"
    assert len(calls)==3


@pytest.mark.parametrize("action",["BREAK_START","BREAK_END"])
def test_manual_break_uses_employee_face_token_without_service_scope_check(monkeypatch,action):
    now=datetime.now(timezone.utc)
    event={"id":"recognition-break","event_type":"PERSON_RECOGNIZED","camera_id":"cam",
           "edge_id":"edge","event_time":now.isoformat(),
           "payload":{"payload":{"person_id":"person","metadata":{}}}}
    calls=[]
    class Store:
        def get_event(self,*_args): return event
        def crm_person_mapping(self,*_args):
            return {"crm_user_id":"employee-a","break_master_id":"lunch"}
        def set_attendance_presence_break(self,*args): calls.append(("local-break",args))
        def record_attendance_activity(self,item): calls.append(("activity",item))
        def record_portal_event(self,item): calls.append(("event",item))

    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_portal_scope",lambda *_args:None)
    monkeypatch.setattr(api,"_portal_camera_lookup",lambda *_args:{
        "camera_role":"ENTRANCE_EXIT","camera_zone":"inside"})
    monkeypatch.setattr(api,"_crm_tenant_uuid_for_user",lambda *_args:"crm-tenant-uuid")
    monkeypatch.setattr(api,"_crm_face_token",lambda *_args,**_kwargs:"face-token")
    monkeypatch.setattr(api,"_crm_face_login_succeeded",lambda _result:True)
    monkeypatch.setattr(api,"_assert_crm_service_token_scope",
                        lambda tenant,user=None:calls.append(("scope",tenant,user)))
    monkeypatch.setattr(api,"_crm_mutation_succeeded",lambda _result:True)
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(
        face_attendance_configured=True,
        login_using_face_tenant=lambda *_args:{"success":True,"token":"face-token",
                                                "user":{"id":"employee-a"}},
        start_break=lambda *args,**kwargs:calls.append(("start",args,kwargs)) or {"success":True},
        end_break=lambda *args,**kwargs:calls.append(("end",args,kwargs)) or {"success":True}))

    result=api.attendance_station_action("tenant",api.AttendanceStationActionRequest(
        camera_id="cam",edge_id="edge",recognition_event_id="recognition-break",action=action),
        PortalPrincipal("session","tenant",None,"shop","admin","Admin","OWNER"))

    assert result["ok"] is True
    operation="start" if action=="BREAK_START" else "end"
    crm_call=next(call for call in calls if call[0]==operation)
    assert crm_call[1][0]=="employee-a"
    assert crm_call[2]=={"auth_token":"face-token"}
    assert not any(call[0]=="scope" for call in calls)
