"""Exercise attendance routes and the encrypted vault against a mocked CRM contract."""
import base64
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
import cv2
import numpy as np
from cryptography.fernet import Fernet
from fastapi import HTTPException

from cloud_portal import api
from cloud_portal.attendance_tokens import decrypt_scoped_token, encrypt_scoped_token
from cloud_portal.crm_client import SnapKeyCrmClient


@pytest.fixture
def employee_crm(monkeypatch):
    now=datetime.now(timezone.utc)
    state=SimpleNamespace(requests=[],face_logins=0,failure=None,face_user="employee-a",message="")
    ok,enrolled=cv2.imencode('.jpg',np.zeros((32,32,3),dtype=np.uint8));assert ok
    state.enrolled_image=base64.b64encode(enrolled).decode()
    evidence={"cloud_evidence_snapshots":[{"evidence_id":f"snapshot-{i}"} for i in range(3)],
              "cloud_clip":{"evidence_id":"clip-1"}}
    event={"event_id":"recognition-a","event_type":"PERSON_RECOGNIZED",
           "tenant_id":"tenant-a","shop_id":"shop-a","edge_id":"edge-a",
           "camera_id":"camera-a","event_time":now.isoformat(),
           "payload":{"payload":{"person_id":"person-a","metadata":evidence}}}

    class Store:
        def __init__(self):
            self.tokens={}; self.activities=[]; self.events=[]; self.local=[]
            self.checked_in=False; self.claimed=False; self.reconciliations=[]
        def get_crm_face_token(self,*scope): return self.tokens.get(scope)
        def save_crm_face_token(self,tenant,shop,user,ciphertext,expires):
            self.tokens[(tenant,shop,user)]={"encrypted_token":ciphertext,"expires_at":expires}
        def delete_crm_face_token(self,*scope): self.tokens.pop(scope,None)
        def crm_person_mapping(self,*_args):
            return {"crm_user_id":"employee-a","break_master_id":"lunch"}
        def list_crm_person_mappings(self,*_args): return []
        def person_attendance_policy(self,*_args): return {"attendanceMode":"AUTO"}
        def get_event(self,*_args): return {**event,"id":event["event_id"]}
        def touch_attendance_presence(self,**kwargs):
            if kwargs.get("checked_in") is not None: self.checked_in=kwargs["checked_in"]
            return {"checked_in":self.checked_in}
        def set_attendance_presence_break(self,*args): self.local.append(("break",args))
        def complete_presence_checkout(self,*args):
            self.local.append(("checkout",args)); self.checked_in=False
        def record_attendance_activity(self,item): self.activities.append(item)
        def record_portal_event(self,item): self.events.append(item)
        def attendance_camera_coverage_status(self,*_args,**_kwargs): return {"state":"HEALTHY"}
        def claim_crm_auto_logout(self,*_args):
            if self.claimed: return False
            self.claimed=True; return True
        def mark_crm_auto_logout_confirmed(self,*_args): return True
        def finalize_crm_auto_logout_local(self,*args):
            self.activities.append(args[-1]); return "SUCCEEDED"
        def mark_crm_auto_logout_reconciliation_required(self,*args):
            self.reconciliations.append(args)

    def handle(request):
        state.requests.append(request)
        path=request.url.path
        if path.startswith("/api/User/face-embeddings/"):
            tenant=request.url.path.rsplit("/",1)[-1]
            users=([{"id":"employee-a","tenantId":"uuid-a","tenantCode":tenant,
                     "name":"Employee A","faceImages":[state.enrolled_image]}]
                   if tenant=="tenant-a" else [{"id":"employee-b","tenantId":"uuid-b"}])
            return httpx.Response(200,json=users)
        if path=="/api/Auth/loginUsingFaceTenant":
            state.face_logins+=1
            exp=int((datetime.now(timezone.utc)+timedelta(hours=2)).timestamp())
            body=base64.urlsafe_b64encode(json.dumps({"exp":exp}).encode()).decode().rstrip("=")
            token=f"a.{body}.mock-signature-{state.face_logins}"
            return httpx.Response(200,json={"success":True,"token":token,"message":state.message,
                "user":{"id":state.face_user,"tenantId":"uuid-a"}})
        if path not in {"/api/UserRoster/LoginLogout","/api/UserBreak/start-break",
                        "/api/UserBreak/end-break","/api/UserActivity/auto-logout"}:
            raise AssertionError("Unexpected CRM endpoint: "+path)
        failure=state.failure; state.failure=None
        if failure=="timeout": raise httpx.ReadTimeout("mock timeout",request=request)
        if failure==401: return httpx.Response(401,json={"success":False})
        if failure=="validation": return httpx.Response(400,json={
            "title":"One or more validation errors occurred.",
            "errors":{"actualStartTime":["The value must be a valid TimeSpan."]},
            "token":"must-not-be-returned"})
        return httpx.Response(200,json={"success":failure!="rejected","message":state.message})

    real_client=httpx.Client
    monkeypatch.setattr(httpx,"Client",lambda *args,**kwargs:
        real_client(*args,transport=httpx.MockTransport(handle),**kwargs))
    monkeypatch.delenv("SNAPKEY_CRM_API_TOKENS_BY_TENANT_JSON",raising=False)
    monkeypatch.delenv("SNAPKEY_CRM_API_TOKEN",raising=False)
    monkeypatch.setenv("CAMERA_EYE_TOKEN_ENCRYPTION_KEY",Fernet.generate_key().decode())
    monkeypatch.setattr(api,"crm_client",SnapKeyCrmClient(base_url="https://crm.invalid"))
    store=Store()
    monkeypatch.setattr(api,"store",store)
    monkeypatch.setattr(api,"_recognition_image_base64",lambda _payload:"live-a")
    monkeypatch.setattr(api,"_portal_scope",lambda *_args:None)
    monkeypatch.setattr(api,"_portal_camera_lookup",lambda *_args:{
        "camera_role":"ENTRANCE_EXIT","camera_zone":"inside"})
    monkeypatch.setattr(api,"_notify_cloud_event",lambda *_args:None)
    principal=api.PortalPrincipal("session","tenant-a",None,"shop-a","admin","Admin","OWNER")
    row={"tenant_id":"tenant-a","shop_id":"shop-a","crm_user_id":"employee-a",
         "local_person_id":"person-a","checked_in":True,"on_break":False,
         "last_seen_at":now-timedelta(minutes=61),"last_camera_id":"camera-a",
         "last_camera_zone":"inside","last_recognition_event_id":"recognition-a",
         "policy_json":{"absenceMonitoringEnabled":True,"markAbsentAfterMinutes":60}}
    return SimpleNamespace(state=state,store=store,event=event,principal=principal,row=row,now=now)


def manual(ctx,action):
    return api.attendance_station_action("tenant-a",api.AttendanceStationActionRequest(
        camera_id="camera-a",edge_id="edge-a",recognition_event_id="recognition-a",action=action),
        ctx.principal)


def mutations(ctx):
    return [r for r in ctx.state.requests if r.method=="POST"
            and r.url.path!="/api/Auth/loginUsingFaceTenant"]


def test_one_cached_employee_token_covers_login_logout_breaks_and_absence(employee_crm,monkeypatch):
    ctx=employee_crm
    monkeypatch.setenv("SNAPKEY_CRM_AUTO_LOGIN_ENABLED","1")
    monkeypatch.setenv("SNAPKEY_CRM_AUTO_LOGOUT_ENABLED","1")
    monkeypatch.setenv("CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED","true")
    api._auto_attend_recognized_person(ctx.event)
    before=len(mutations(ctx))
    api._auto_attend_recognized_person(ctx.event)
    assert len(mutations(ctx))==before  # duplicate recognition in an open session
    for action in ("BREAK_START","BREAK_END","CHECK_OUT","CHECK_IN"):
        assert manual(ctx,action)["ok"] is True
    for event_type in ("ATTENDANCE_ENTRY","ATTENDANCE_EXIT","BREAK_START","BREAK_END"):
        api._deliver_crm_attendance_event({**ctx.event,"event_type":event_type,
            "payload":{"payload":{"person_id":"person-a","metadata":{"crm_confirmed_break":True}}}})
    api._process_automatic_checkout(ctx.row,now=ctx.now,reason_code="MAX_LOGOFF_REACHED",
                                   require_camera_health=False)
    api._v2_auto_logout(ctx.row,ctx.now)

    assert ctx.state.face_logins==1
    assert len(mutations(ctx))==11
    assert {r.url.path for r in mutations(ctx)}=={
        "/api/UserRoster/LoginLogout","/api/UserBreak/start-break",
        "/api/UserBreak/end-break","/api/UserActivity/auto-logout"}
    tokens={r.headers["Authorization"] for r in mutations(ctx)}
    assert len(tokens)==1
    cached=ctx.store.tokens[("tenant-a","shop-a","employee-a")]
    assert decrypt_scoped_token(cached["encrypted_token"],"tenant-a","shop-a","employee-a")==tokens.pop()
    assert ctx.store.activities[0]["evidence"]["status"]=="COMPLETE"
    assert not api.crm_client.configured  # no service credential was needed


def test_expired_token_is_renewed_for_unattended_absence_without_live_image(employee_crm,monkeypatch):
    ctx=employee_crm
    manual(ctx,"CHECK_IN")
    ctx.store.tokens[("tenant-a","shop-a","employee-a")]["expires_at"]=ctx.now-timedelta(seconds=1)
    monkeypatch.setenv("CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED","true")
    api._v2_auto_logout(ctx.row,ctx.now)
    auth=[r for r in ctx.state.requests if r.url.path=="/api/Auth/loginUsingFaceTenant"]
    assert len(auth)==2
    assert json.loads(auth[0].content)["base64Image"]==ctx.state.enrolled_image
    assert json.loads(auth[1].content)=={"base64Image":ctx.state.enrolled_image,"tenantId":"uuid-a"}
    assert mutations(ctx)[-1].url.path=="/api/UserActivity/auto-logout"


def test_manual_401_invalidates_only_employee_token_and_does_not_replay(employee_crm):
    ctx=employee_crm
    manual(ctx,"CHECK_IN")
    ctx.store.tokens[("tenant-a","shop-a","employee-b")]={"encrypted_token":"untouched"}
    local_before=list(ctx.store.local); activities_before=len(ctx.store.activities)
    ctx.state.failure=401
    with pytest.raises(HTTPException) as exc:
        manual(ctx,"BREAK_START")
    assert exc.value.status_code==502
    assert len(mutations(ctx))==2 and ctx.state.face_logins==1
    assert ("tenant-a","shop-a","employee-a") not in ctx.store.tokens
    assert ctx.store.tokens[("tenant-a","shop-a","employee-b")]=={"encrypted_token":"untouched"}
    assert ctx.store.local==local_before and len(ctx.store.activities)==activities_before
    assert manual(ctx,"BREAK_START")["ok"] is True  # a separate, explicit retry
    assert ctx.state.face_logins==2 and len(mutations(ctx))==3


def test_manual_attendance_displays_safe_crm_validation_details(employee_crm):
    ctx=employee_crm; ctx.state.failure="validation"

    with pytest.raises(HTTPException) as exc:
        manual(ctx,"CHECK_IN")

    assert exc.value.status_code==502
    assert "actualStartTime: The value must be a valid TimeSpan." in exc.value.detail
    assert "must-not-be-returned" not in exc.value.detail


@pytest.mark.parametrize("action",["CHECK_IN","CHECK_OUT","BREAK_START","BREAK_END"])
def test_business_rejection_never_updates_local_attendance(employee_crm,action):
    ctx=employee_crm; ctx.state.failure="rejected"
    with pytest.raises(HTTPException) as exc:
        manual(ctx,action)
    assert exc.value.status_code==409
    assert ctx.store.local==[] and ctx.store.activities==[] and ctx.store.events==[]


def test_absence_timeout_preserves_reconciliation_and_never_replays(employee_crm,monkeypatch):
    ctx=employee_crm; ctx.state.failure="timeout"
    monkeypatch.setenv("CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED","true")
    api._v2_auto_logout(ctx.row,ctx.now)
    api._v2_auto_logout(ctx.row,ctx.now)
    assert len(mutations(ctx))==1 and len(ctx.store.reconciliations)==1
    assert ctx.store.activities==[]


def test_cached_token_cannot_authorize_user_outside_requested_tenant(employee_crm):
    ctx=employee_crm
    manual(ctx,"CHECK_IN")
    with pytest.raises(RuntimeError,match="requested CRM user"):
        api._crm_face_token("tenant-b","shop-a","employee-a")
    assert len(mutations(ctx))==1 and ctx.state.face_logins==1


def test_mismatched_face_identity_is_not_cached_or_used(employee_crm):
    ctx=employee_crm; ctx.state.face_user="employee-b"
    with pytest.raises(HTTPException) as exc:
        manual(ctx,"CHECK_IN")
    assert exc.value.status_code==409
    assert ctx.store.tokens=={} and mutations(ctx)==[]


def test_directory_user_list_needs_no_static_token_and_omits_biometrics(employee_crm):
    ctx=employee_crm
    api.crm_client.token="unrelated-tenant-service-token"
    result=api.crm_users("tenant-a",ctx.principal)
    assert result["items"][0]["id"]=="employee-a"
    assert "faceImages" not in result["items"][0]
    assert len(ctx.state.requests)==1
    assert "Authorization" not in ctx.state.requests[0].headers
    with pytest.raises(HTTPException) as exc:
        api.crm_face_embeddings("tenant-a","tenant-b",ctx.principal)
    assert exc.value.status_code==403 and len(ctx.state.requests)==1


def test_cached_manual_action_needs_no_new_image_or_face_login(employee_crm,monkeypatch):
    ctx=employee_crm
    manual(ctx,"CHECK_IN")
    monkeypatch.setattr(api,"_recognition_image_base64",lambda *_args:pytest.fail("cache reuse needs no image"))
    assert manual(ctx,"BREAK_START")["ok"] is True
    assert ctx.state.face_logins==1


def test_cached_token_does_not_bypass_recognition_expiry(employee_crm):
    ctx=employee_crm
    manual(ctx,"CHECK_IN")
    ctx.event["event_time"]=(ctx.now-timedelta(minutes=1)).isoformat()
    with pytest.raises(HTTPException) as exc:
        manual(ctx,"BREAK_START")
    assert exc.value.status_code==409 and len(mutations(ctx))==1


def test_encryption_key_is_required_before_any_attendance_mutation(employee_crm,monkeypatch):
    ctx=employee_crm
    monkeypatch.delenv("CAMERA_EYE_TOKEN_ENCRYPTION_KEY")
    with pytest.raises(HTTPException) as exc:
        manual(ctx,"CHECK_IN")
    assert exc.value.status_code==502
    assert mutations(ctx)==[] and ctx.store.tokens=={} and ctx.store.activities==[]


def test_admin_break_test_routes_use_the_same_employee_token(employee_crm):
    ctx=employee_crm
    assert api.crm_test_break_start("tenant-a","person-a",ctx.principal)["result"]["success"]
    assert api.crm_test_break_end("tenant-a","person-a",ctx.principal)["result"]["success"]
    assert ctx.state.face_logins==1
    assert len(mutations(ctx))==2
    assert len({r.headers["Authorization"] for r in mutations(ctx)})==1


def test_employee_token_and_images_are_absent_from_logs(employee_crm,caplog):
    ctx=employee_crm
    ctx.state.message="untrusted CRM message containing private-token-and-image"
    manual(ctx,"CHECK_IN")
    manual(ctx,"BREAK_START")
    token=mutations(ctx)[0].headers["Authorization"]
    assert token not in caplog.text
    assert "live-a" not in caplog.text and ctx.state.enrolled_image not in caplog.text
    assert ctx.state.message not in caplog.text


def test_cached_jwt_expiry_is_respected_even_if_database_expiry_is_later(employee_crm):
    ctx=employee_crm
    manual(ctx,"CHECK_IN")
    expired=int((ctx.now-timedelta(minutes=1)).timestamp())
    body=base64.urlsafe_b64encode(json.dumps({"exp":expired}).encode()).decode().rstrip("=")
    cached=ctx.store.tokens[("tenant-a","shop-a","employee-a")]
    cached["encrypted_token"]=encrypt_scoped_token(f"a.{body}.expired","tenant-a","shop-a","employee-a")
    assert cached["expires_at"]>ctx.now
    manual(ctx,"CHECK_OUT")
    assert ctx.state.face_logins==2


def test_concurrent_token_requests_single_face_login(employee_crm):
    from concurrent.futures import ThreadPoolExecutor
    ctx=employee_crm
    with ThreadPoolExecutor(max_workers=6) as executor:
        results=list(executor.map(lambda _:api._crm_face_token("tenant-a","shop-a","employee-a"),range(12)))
    assert len(set(results))==1
    assert ctx.state.face_logins==1

def test_face_directory_concurrent_cache_miss_single_request(monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    client=SnapKeyCrmClient(); calls=[];entered=threading.Event();release=threading.Event()
    def request(*args,**kwargs):
        calls.append(1);entered.set();release.wait(2);return []
    monkeypatch.setattr(client,'_request',request)
    with ThreadPoolExecutor(max_workers=6) as executor:
        futures=[executor.submit(client.face_embeddings,'tenant-a') for _ in range(6)]
        assert entered.wait(2);release.set()
        assert [future.result() for future in futures]==[[]]*6
    assert calls==[1]

def test_auth_directory_outage_cannot_use_stale_membership(monkeypatch):
    import httpx,time
    client=SnapKeyCrmClient()
    client._face_directory_cache['tenant-a']=(time.monotonic()-client.face_directory_ttl_seconds-1,[{'id':'old'}])
    def fail(*args,**kwargs):raise httpx.ConnectError('mock offline')
    monkeypatch.setattr(client,'_request',fail)
    assert client.face_embeddings('tenant-a')==[{'id':'old'}]
    with pytest.raises(httpx.ConnectError):client.face_embeddings('tenant-a',allow_stale=False)
