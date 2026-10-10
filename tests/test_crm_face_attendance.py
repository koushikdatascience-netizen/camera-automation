import httpx
import pytest
from datetime import datetime,timezone
import json
from pathlib import Path

from cloud_portal.crm_client import CrmUnconfirmedMutationResponse, SnapKeyCrmClient
from cloud_portal.crm_attendance_contract import format_crm_attendance_date_time


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload=payload
        self.status_code=status_code
        self.content=b"{}"
        self.text="{}"

    def raise_for_status(self):
        if self.status_code >= 400:
            request=httpx.Request("GET","https://apis.snapkey.in/test")
            response=httpx.Response(self.status_code,request=request)
            raise httpx.HTTPStatusError("error",request=request,response=response)

    def json(self):
        return self._payload


class FakeClient:
    calls=[]

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def request(self, method, path, headers=None, **kwargs):
        self.calls.append({"method":method,"path":path,"headers":headers or {},"kwargs":kwargs})
        if method.upper()=="POST":
            return FakeResponse({"success":True})
        return FakeResponse([{"id":"crm-user-1","tenantId":"tenant-uuid-1"}])

    def post(self, url, headers=None, json=None, **kwargs):
        self.calls.append({"method":"POST","path":url,"headers":headers or {},"json":json})
        return FakeResponse({"success":True,"token":"face-token","user":{"id":"crm-user-1"}})


def test_face_directory_is_public_and_cached_for_ttl(monkeypatch):
    FakeClient.calls=[]
    monkeypatch.setattr(httpx,"Client",FakeClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in",token="expired-static-token")
    client.face_directory_ttl_seconds=60

    first=client.face_embeddings("ABM-46-775")
    second=client.face_embeddings("ABM-46-775")

    assert first == second
    assert len(FakeClient.calls) == 1
    call=FakeClient.calls[0]
    assert call["path"] == "/api/User/face-embeddings/ABM-46-775"
    assert "Authorization" not in call["headers"]


def test_crm_attendance_date_and_time_use_effective_business_timezone(monkeypatch):
    event_time=datetime(2026,10,7,20,0,tzinfo=timezone.utc)

    actual_date,actual_time=format_crm_attendance_date_time(
        event_time,"Asia/Kolkata")

    assert actual_date=="2026-10-08T01:30:00.000+05:30"
    assert actual_time=="01:30:00"


def test_crm_attendance_login_fixture_has_exact_n8n_datetime_and_timespan():
    fixture=json.loads((Path(__file__).parent/'fixtures'/'crm_login_logout_success.json').read_text())
    request=fixture['request']
    assert request=={
        'userId':'<CRM_USER_UUID>',
        'date':'2026-10-10T17:06:17.770+05:30',
        'actualStartTime':'17:06:17',
    }
    assert fixture['response']=={'success':True,'message':'Logged in successfully.'}
    start=datetime.fromisoformat(request['date'])
    assert start.utcoffset().total_seconds()==19800
    assert format_crm_attendance_date_time(datetime(2026,10,10,11,36,17,770000,timezone.utc),
                                           'Asia/Kolkata')==(
        request['date'],request['actualStartTime'])


def test_login_logout_requires_exact_success_true_contract(monkeypatch):
    class ContractClient(FakeClient):
        payload={'success':True,'message':'Logged in successfully.'}
        def request(self,method,path,headers=None,**kwargs):
            self.calls.append({'method':method,'path':path,'headers':headers or {},'kwargs':kwargs})
            return FakeResponse(self.payload)
    ContractClient.calls=[]
    monkeypatch.setattr(httpx,'Client',ContractClient)
    client=SnapKeyCrmClient(base_url='https://apis.snapkey.in')
    fixture=json.loads((Path(__file__).parent/'fixtures'/'crm_login_logout_success.json').read_text())
    assert client.login_logout_with_face_token(fixture['request'],'raw-face-token')==fixture['response']
    request=ContractClient.calls[0]
    assert request['headers']['Authorization']=='raw-face-token'
    assert request['kwargs']['json']==fixture['request']
    ContractClient.payload={'ok':True,'message':'not the confirmed contract'}
    with pytest.raises(CrmUnconfirmedMutationResponse):
        client.login_logout_with_face_token(fixture['request'],'raw-face-token')
    ContractClient.payload={'message':'accepted without success flag'}
    with pytest.raises(CrmUnconfirmedMutationResponse):
        client.login_logout_with_face_token(fixture['request'],'raw-face-token')


def test_crm_error_details_include_dotnet_field_errors_and_redact_credentials():
    detail=SnapKeyCrmClient.safe_error_message({
        "title":"One or more validation errors occurred.",
        "status":400,
        "errors":{"actualStartTime":["The value must be a valid TimeSpan."],
                  "passwordHash":["private-hash"]},
        "token":"private-token",
    })

    assert "actualStartTime: The value must be a valid TimeSpan." in detail
    assert "400" in detail
    assert "private-hash" not in detail and "private-token" not in detail


def test_face_directory_force_refresh_bypasses_cache(monkeypatch):
    FakeClient.calls=[]
    monkeypatch.setattr(httpx,"Client",FakeClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in",token="expired-static-token")

    client.face_embeddings("ABM-46-775")
    client.face_embeddings("ABM-46-775",force_refresh=True)

    assert len(FakeClient.calls) == 2
    assert all("Authorization" not in call["headers"] for call in FakeClient.calls)


def test_login_using_face_tenant_does_not_send_static_token(monkeypatch):
    FakeClient.calls=[]
    monkeypatch.setattr(httpx,"Client",FakeClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in",token="expired-static-token")

    result=client.login_using_face_tenant("data:image/jpeg;base64,ZmFrZQ==","tenant-uuid-1")

    assert result["success"] is True
    call=FakeClient.calls[0]
    assert call["path"].endswith("/api/Auth/loginUsingFaceTenant")
    assert "Authorization" not in call["headers"]
    assert call["json"] == {"base64Image":"ZmFrZQ==","tenantId":"tenant-uuid-1"}


def test_face_token_is_used_for_login_logout(monkeypatch):
    FakeClient.calls=[]
    monkeypatch.setattr(httpx,"Client",FakeClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in",token="expired-static-token")

    client.login_logout_with_face_token(
        {"userId":"crm-user-1","date":"2026-10-07","actualStartTime":"09:00:00"},
        "face-token",
    )

    call=FakeClient.calls[0]
    assert call["path"] == "/api/UserRoster/LoginLogout"
    assert call["headers"]["Authorization"] == "face-token"
    assert "expired-static-token" not in str(call)


@pytest.mark.parametrize("body", [b"", b"upstream proxy generated this page"])
def test_attendance_mutation_without_json_confirmation_is_uncertain(monkeypatch, body):
    class UnconfirmedResponse(FakeResponse):
        def __init__(self, payload=None, status_code=200):
            super().__init__(payload, status_code)
            self.content=body
            self.text=body.decode("ascii")

        def json(self):
            if body:
                raise ValueError("not JSON")
            return super().json()

    class UnconfirmedClient(FakeClient):
        def request(self, method, path, headers=None, **kwargs):
            self.calls.append({"method":method,"path":path,"headers":headers or {},"kwargs":kwargs})
            return UnconfirmedResponse()

    UnconfirmedClient.calls=[]
    monkeypatch.setattr(httpx,"Client",UnconfirmedClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in")

    with pytest.raises(CrmUnconfirmedMutationResponse):
        client.login_logout_with_face_token(
            {"userId":"crm-user-1","date":"2026-10-07","actualStartTime":"09:00:00"},
            "face-token",
        )

    assert len(UnconfirmedClient.calls)==1
    assert UnconfirmedClient.calls[0]["path"]=="/api/UserRoster/LoginLogout"


def test_attendance_mutation_with_unconfirmed_business_body_is_uncertain(monkeypatch):
    class UnconfirmedClient(FakeClient):
        def request(self, method, path, headers=None, **kwargs):
            self.calls.append({"method":method,"path":path,"headers":headers or {},"kwargs":kwargs})
            return FakeResponse({"message":"request accepted"})

    UnconfirmedClient.calls=[]
    monkeypatch.setattr(httpx,"Client",UnconfirmedClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in")

    with pytest.raises(CrmUnconfirmedMutationResponse):
        client.login_logout_with_face_token(
            {"userId":"crm-user-1","date":"2026-10-07","actualStartTime":"09:00:00"},
            "face-token",
        )


def test_auto_logout_uses_distinct_endpoint_and_exact_payload(monkeypatch):
    class AutoLogoutClient(FakeClient):
        def request(self, method, path, headers=None, **kwargs):
            self.calls.append({"method":method,"path":path,"headers":headers or {},"kwargs":kwargs})
            return FakeResponse({"success":True})

    AutoLogoutClient.calls=[]
    monkeypatch.setattr(httpx,"Client",AutoLogoutClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in",token="expired-static-token")

    result=client.auto_logout_with_face_token(
        "crm-user-1","AUTO_LOGOUT: absent for 60 minutes","face-token")

    assert result == {"success":True}
    call=AutoLogoutClient.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/UserActivity/auto-logout"
    assert call["headers"]["Authorization"] == "Bearer face-token"
    assert call["kwargs"]["json"] == {
        "userId":"crm-user-1","remarks":"AUTO_LOGOUT: absent for 60 minutes"}
    assert "expired-static-token" not in str(call)


def test_monthly_roster_uses_only_the_configured_tenant_service_token(monkeypatch):
    FakeClient.calls=[]
    monkeypatch.setenv("SNAPKEY_CRM_API_TOKENS_BY_TENANT_JSON",
                       '{"tenant-a":"service-a","tenant-b":"service-b"}')
    monkeypatch.setattr(httpx,"Client",FakeClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in",token="legacy-token")

    client.users_roster(2026,10,tenant_code="TENANT-B")

    call=FakeClient.calls[0]
    assert call["path"]=="/api/UserRoster/GetUsersRoster"
    assert call["headers"]["Authorization"]=="Bearer service-b"
    assert "legacy-token" not in str(call)
    with pytest.raises(RuntimeError,match="No CRM service token"):
        client.users_roster(2026,10,tenant_code="tenant-c")


def test_tenant_service_token_configuration_rejects_invalid_json_without_echoing_it(monkeypatch):
    monkeypatch.setenv("SNAPKEY_CRM_API_TOKENS_BY_TENANT_JSON","not-a-json-secret")

    with pytest.raises(RuntimeError,match="must be valid JSON") as error:
        SnapKeyCrmClient(token="legacy-token")

    assert "not-a-json-secret" not in str(error.value)


def test_break_mutation_uses_employee_face_token_not_service_token(monkeypatch):
    FakeClient.calls=[]
    monkeypatch.setenv("SNAPKEY_CRM_API_TOKENS_BY_TENANT_JSON",
                       '{"tenant-a":"service-a"}')
    monkeypatch.setattr(httpx,"Client",FakeClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in",token="legacy-token")

    client.start_break("employee-a","break-lunch",auth_token="employee-face-token")

    call=FakeClient.calls[0]
    assert call["path"]=="/api/UserBreak/start-break"
    assert call["headers"]["Authorization"]=="employee-face-token"
    assert call["kwargs"]["json"]=={"userId":"employee-a","breakMasterId":"break-lunch"}
    assert "legacy-token" not in str(call)


def test_tenant_token_map_never_falls_back_to_legacy_token(monkeypatch):
    monkeypatch.setenv("SNAPKEY_CRM_API_TOKENS_BY_TENANT_JSON",'{"tenant-a":"tenant-a-token"}')
    client=SnapKeyCrmClient(token="legacy-token")

    with pytest.raises(RuntimeError,match="Tenant code is required"):
        client.service_token_for_tenant()
    with pytest.raises(RuntimeError,match="No CRM service token"):
        client.service_token_for_tenant("tenant-b")
    assert client.service_token_for_tenant("tenant-a")=="tenant-a-token"


@pytest.mark.parametrize("operation",["login","logout","absence","break-start","break-end"])
def test_employee_operations_reject_empty_token_without_service_fallback(monkeypatch,operation):
    FakeClient.calls=[]
    monkeypatch.setattr(httpx,"Client",FakeClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in",token="legacy-token")
    calls={
        "login":lambda:client.login_logout_with_face_token({"userId":"employee-a","actualStartTime":"09:00"},""),
        "logout":lambda:client.login_logout_with_face_token({"userId":"employee-a","actualOffTime":"18:00"},""),
        "absence":lambda:client.auto_logout_with_face_token("employee-a","absence",""),
        "break-start":lambda:client.start_break("employee-a","lunch",auth_token=""),
        "break-end":lambda:client.end_break("employee-a",auth_token=""),
    }
    with pytest.raises(ValueError,match="token is required"):
        calls[operation]()
    assert FakeClient.calls==[]
