import httpx

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
