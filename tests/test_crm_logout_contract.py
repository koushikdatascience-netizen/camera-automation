"""Mock-only endpoint and absence safety contracts; never contacts CRM."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import httpx
import pytest
from cloud_portal import api
from cloud_portal.crm_client import SnapKeyCrmClient, CrmUnconfirmedMutationResponse
from test_crm_face_attendance import FakeClient, FakeResponse

@pytest.mark.parametrize("result", [{"ok": True}, "success", {"message": "success"}, {"success": 1}])
def test_auto_logout_unverified_success_is_uncertain(monkeypatch, result):
    class Client(FakeClient):
        def request(self, *args, **kwargs): return FakeResponse(result)
    monkeypatch.setattr(httpx, "Client", Client)
    with pytest.raises(CrmUnconfirmedMutationResponse):
        SnapKeyCrmClient().auto_logout_with_face_token("employee", "policy absence", "token")

@pytest.mark.parametrize("zone,date,off", [
    ("Asia/Kolkata", "2026-10-11T00:30:00.000+05:30", "00:30:00"),
    ("America/New_York", "2026-10-10T15:00:00.000-04:00", "15:00:00")])
def test_manual_bridge_logout_exact_payload(monkeypatch, zone, date, off):
    calls=[]
    monkeypatch.setattr(api, "store", SimpleNamespace(
        crm_person_mapping=lambda *args: {"crm_user_id":"employee"},
        person_attendance_policy=lambda *args: {"attendanceMode":"MANUAL","timezone":zone}))
    monkeypatch.delenv("SNAPKEY_CRM_ATTENDANCE_TIMEZONE", raising=False)
    monkeypatch.setattr(api, "_crm_face_token", lambda *args:"raw-token")
    monkeypatch.setattr(api, "crm_client", SimpleNamespace(login_logout_with_face_token=
        lambda payload,token: calls.append((payload,token)) or {"success":True,"message":"Logged out."}))
    envelope={"event_type":"ATTENDANCE_EXIT","tenant_id":"tenant","shop_id":"shop",
        "event_time":"2026-10-10T19:00:00+00:00", "payload":{"person_id":"person",
        "metadata":{"attendance_sync_bridge":True,"attendance_source":"MANUAL"}}}
    assert api._deliver_crm_attendance_event(envelope)["success"] is True
    assert calls==[({"userId":"employee","date":date,"actualOffTime":off},"raw-token")]

@pytest.mark.parametrize("age,health,result,expected", [
    (89,"HEALTHY",{"success":True},[]),
    (90,"UNKNOWN",{"success":True},[]),
    (90,"HEALTHY",{"ok":True},["crm","reconcile"]),
    (90,"HEALTHY",{"success":False,"message":"Denied"},["crm","retry"]),
    (90,"HEALTHY",{"success":True,"message":"Done"},["crm","confirmed","finalized"])])
def test_policy_absence_and_uncertain_result(monkeypatch,age,health,result,expected):
    now=datetime(2026,10,10,20,tzinfo=timezone.utc); calls=[]; captured=[]
    def confirmed(*args):
        calls.append("confirmed"); captured.append(args[-1]); return True
    def crm(user,remarks,token):
        calls.append("crm"); assert "90 minutes" in remarks; return result
    monkeypatch.setenv("CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED","true")
    monkeypatch.setattr(api,"store",SimpleNamespace(
        attendance_camera_coverage_status=lambda *args,**kwargs:{"state":health},
        claim_crm_auto_logout=lambda *args:True,
        release_crm_auto_logout_for_retry=lambda *args:calls.append("retry"),
        mark_crm_auto_logout_reconciliation_required=lambda *args:calls.append("reconcile"),
        mark_crm_auto_logout_confirmed=confirmed,
        finalize_crm_auto_logout_local=lambda *args:calls.append("finalized") or "SUCCEEDED"))
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(auto_logout_with_face_token=crm))
    monkeypatch.setattr(api,"_crm_face_token",lambda *args:"token")
    monkeypatch.setattr(api,"_evidence_manifest_for_last_recognition",lambda *args:{"status":"UNAVAILABLE"})
    row={"tenant_id":"tenant","shop_id":"shop","crm_user_id":"employee","checked_in":True,
        "on_break":False,"last_seen_at":now-timedelta(minutes=age),"last_camera_id":"camera",
        "policy_json":{"attendanceMode":"MANUAL","markAbsentAfterMinutes":90}}
    api._v2_auto_logout(row,now)
    assert calls==expected
    if captured:
        assert captured[0]["crm_response_message"]=="Done"
        assert captured[0]["policy_snapshot"]==row["policy_json"]
        assert captured[0]["occurred_at"]==now.isoformat()

def test_legacy_absence_never_uses_normal_logout_endpoint(monkeypatch):
    calls=[]
    monkeypatch.setenv("SNAPKEY_CRM_AUTO_LOGOUT_ENABLED","1")
    monkeypatch.setattr(api,"store",SimpleNamespace(complete_presence_checkout=lambda *args:calls.append(args)))
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(login_logout_with_face_token=lambda *args:pytest.fail("wrong endpoint")))
    api._process_automatic_checkout({"tenant_id":"tenant","shop_id":"shop","local_person_id":"person",
        "crm_user_id":"employee"},now=datetime.now(timezone.utc),reason_code="ABSENCE_GRACE_EXCEEDED",require_camera_health=True)
    assert calls==[("tenant","shop","person",False)]

@pytest.mark.parametrize("failure",["timeout","local_commit"])
def test_max_logoff_uncertainty_never_replays_crm(monkeypatch,failure):
    calls=[];state={"value":"PENDING"}
    now=datetime.now(timezone.utc)
    class Store:
        def claim_crm_auto_logout(self,*args,**kwargs):
            if state["value"]!="PENDING": return False
            state["value"]="IN_FLIGHT"; return True
        def attendance_policy(self,*args): return {"timezone":"Asia/Kolkata"}
        def complete_presence_checkout(self,*args): pass
        def mark_crm_auto_logout_confirmed(self,*args):
            state["value"]="CRM_CONFIRMED_LOCAL_PENDING"; return True
        def mark_crm_auto_logout_reconciliation_required(self,*args): state["value"]="RECONCILIATION_REQUIRED"
        def finalize_crm_auto_logout_local(self,*args): raise RuntimeError("local commit unavailable")
    def crm(*args):
        calls.append("crm")
        if failure=="timeout": raise httpx.ReadTimeout("uncertain")
        return {"success":True,"message":"Done"}
    monkeypatch.setenv("SNAPKEY_CRM_AUTO_LOGOUT_ENABLED","1")
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(login_logout_with_face_token=crm))
    monkeypatch.setattr(api,"_crm_face_token",lambda *args:"token")
    monkeypatch.setattr(api,"_evidence_manifest_for_last_recognition",lambda *args:{"status":"UNAVAILABLE"})
    row={"tenant_id":"tenant","shop_id":"shop","crm_user_id":"employee","local_person_id":"person",
        "last_seen_at":now,"last_camera_id":"cam"}
    for _ in range(2):
        api._process_automatic_checkout(row,now=now,reason_code="MAX_LOGOFF_REACHED",require_camera_health=False)
    assert calls==["crm"]
    assert state["value"]==("RECONCILIATION_REQUIRED" if failure=="timeout" else "CRM_CONFIRMED_LOCAL_PENDING")
