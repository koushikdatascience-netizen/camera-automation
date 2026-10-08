import httpx
import pytest

from cloud_portal.crm_client import SnapKeyCrmClient


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
    assert call["headers"]["Authorization"] == "face-token"
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


def test_break_mutation_uses_tenant_scoped_service_token_not_face_token(monkeypatch):
    FakeClient.calls=[]
    monkeypatch.setenv("SNAPKEY_CRM_API_TOKENS_BY_TENANT_JSON",
                       '{"tenant-a":"service-a"}')
    monkeypatch.setattr(httpx,"Client",FakeClient)
    client=SnapKeyCrmClient(base_url="https://apis.snapkey.in",token="legacy-token")

    client.start_break("employee-a","break-lunch",tenant_code="tenant-a")

    call=FakeClient.calls[0]
    assert call["path"]=="/api/UserBreak/start-break"
    assert call["headers"]["Authorization"]=="Bearer service-a"
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
