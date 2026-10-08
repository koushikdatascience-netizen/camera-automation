from datetime import datetime, timedelta, timezone
import base64
import json
import pytest

from cryptography.fernet import Fernet
from cloud_portal.attendance_tokens import (
    encrypt_token,decrypt_token,encrypt_scoped_token,decrypt_scoped_token,
    TokenScopeError,jwt_expiry,usable,
)


def test_token_encrypt_roundtrip(monkeypatch):
    monkeypatch.setenv("CAMERA_EYE_TOKEN_ENCRYPTION_KEY",Fernet.generate_key().decode())
    ciphertext=encrypt_token("secret-value")
    assert ciphertext!="secret-value"
    assert decrypt_token(ciphertext)=="secret-value"


def test_encryption_requires_key(monkeypatch):
    monkeypatch.delenv("CAMERA_EYE_TOKEN_ENCRYPTION_KEY",raising=False)
    with pytest.raises(RuntimeError):
        encrypt_token("secret")


def test_jwt_exp_is_earlier_than_24h_contract():
    now=datetime(2026,10,8,tzinfo=timezone.utc)
    exp=int((now+timedelta(hours=2)).timestamp())
    body=base64.urlsafe_b64encode(json.dumps({"exp":exp}).encode()).decode().rstrip("=")
    token="a."+body+".c"
    assert jwt_expiry(token,issued_at=now)==now+timedelta(hours=2)
    assert not usable(now+timedelta(minutes=4),now)
    assert usable(now+timedelta(minutes=6),now)


def test_cached_face_token_is_tenant_user_keyed_encrypted_and_refreshed_when_expired(monkeypatch):
    from types import SimpleNamespace
    from cloud_portal import api

    key=Fernet.generate_key().decode()
    monkeypatch.setenv("CAMERA_EYE_TOKEN_ENCRYPTION_KEY",key)
    now=datetime.now(timezone.utc)
    exp=int((now+timedelta(hours=3)).timestamp())
    payload=base64.urlsafe_b64encode(json.dumps({"exp":exp}).encode()).decode().rstrip("=")
    fresh_token=f"a.{payload}.c"
    expired_token=f"a.{base64.urlsafe_b64encode(json.dumps({'exp':int((now-timedelta(hours=1)).timestamp())}).encode()).decode().rstrip('=')}.c"
    calls=[]
    class Store:
        def get_crm_face_token(self,*args):
            calls.append(("get",args));return {"encrypted_token":encrypt_scoped_token(
                expired_token,"tenant-code","shop-a","employee-a"),
                                                  "expires_at":now-timedelta(minutes=1)}
        def delete_crm_face_token(self,*args): calls.append(("delete",args))
        def save_crm_face_token(self,*args): calls.append(("save",args))
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_crm_tenant_uuid_for_user",lambda *_args:"crm-tenant-uuid")
    monkeypatch.setattr(api,"_crm_face_login_identity",lambda tenant,user:
                        ("crm-tenant-uuid","redacted-enrolled-image") if (tenant,user)==("tenant-code","employee-a")
                        else (_ for _ in ()).throw(AssertionError("wrong tenant/user lookup")))
    monkeypatch.setattr(api,"_crm_face_login_succeeded",lambda result:result.get("success") is True)
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(login_using_face_tenant=lambda image,tenant:
        {"success":True,"token":fresh_token,"user":{"id":"employee-a","tenantId":tenant}}))

    token=api._crm_face_token("tenant-code","shop-a","employee-a")

    assert token==fresh_token
    assert calls[0]==("get",("tenant-code","shop-a","employee-a"))
    assert calls[1]==("delete",("tenant-code","shop-a","employee-a"))
    saved=calls[2][1]
    assert saved[:3]==("tenant-code","shop-a","employee-a")
    assert decrypt_scoped_token(saved[3],"tenant-code","shop-a","employee-a")==fresh_token
    assert saved[4]==datetime.fromtimestamp(exp,timezone.utc)


def test_face_login_rejects_cross_user_or_cross_tenant_response(monkeypatch):
    from cloud_portal import api

    monkeypatch.setattr(api,"_crm_face_login_succeeded",lambda result:result.get("success") is True)
    with pytest.raises(RuntimeError,match="identity mismatch"):
        api._validated_crm_face_login_token(
            {"success":True,"token":"discarded","user":{"id":"employee-b"}},
            "employee-a","tenant-a")
    with pytest.raises(RuntimeError,match="tenant mismatch"):
        api._validated_crm_face_login_token(
            {"success":True,"token":"discarded","user":{"id":"employee-a","tenantId":"tenant-b"}},
            "employee-a","tenant-a")


def test_encrypted_token_cannot_be_reused_under_another_tenant_or_user(monkeypatch):
    monkeypatch.setenv("CAMERA_EYE_TOKEN_ENCRYPTION_KEY",Fernet.generate_key().decode())
    ciphertext=encrypt_scoped_token("private-token","tenant-a","shop-a","employee-a")

    assert decrypt_scoped_token(ciphertext,"tenant-a","shop-a","employee-a")=="private-token"
    for scope in (("tenant-b","shop-a","employee-a"),
                  ("tenant-a","shop-b","employee-a"),
                  ("tenant-a","shop-a","employee-b")):
        with pytest.raises(TokenScopeError):
            decrypt_scoped_token(ciphertext,*scope)


def test_v2_cache_rejects_another_employees_token_and_authenticates_requested_user(monkeypatch):
    from types import SimpleNamespace
    from cloud_portal import api

    monkeypatch.setenv("CAMERA_EYE_TOKEN_ENCRYPTION_KEY",Fernet.generate_key().decode())
    now=datetime.now(timezone.utc)
    valid_exp=int((now+timedelta(hours=2)).timestamp())
    jwt_body=base64.urlsafe_b64encode(json.dumps({"exp":valid_exp}).encode()).decode().rstrip("=")
    refreshed=f"a.{jwt_body}.c"
    wrong_scope=encrypt_scoped_token("other-token","tenant-a","shop-a","employee-b")
    calls=[]
    class Store:
        def get_crm_face_token(self,*args):
            calls.append(("get",args));return {"encrypted_token":wrong_scope,
                                                   "expires_at":now+timedelta(hours=1)}
        def delete_crm_face_token(self,*args): calls.append(("delete",args))
        def save_crm_face_token(self,*args): calls.append(("save",args))
    monkeypatch.setattr(api,"store",Store())
    monkeypatch.setattr(api,"_crm_tenant_uuid_for_user",lambda *_args:"tenant-a-uuid")
    monkeypatch.setattr(api,"_crm_face_login_identity",lambda tenant,user:
                        ("tenant-a-uuid","redacted-image") if user=="employee-a"
                        else (_ for _ in ()).throw(AssertionError("wrong CRM user requested")))
    monkeypatch.setattr(api,"_crm_face_login_succeeded",lambda result:result.get("success") is True)
    monkeypatch.setattr(api,"crm_client",SimpleNamespace(login_using_face_tenant=lambda _image,tenant:
        {"success":True,"token":refreshed,"user":{"id":"employee-a","tenantId":tenant}}))

    assert api._crm_face_token("tenant-a","shop-a","employee-a")==refreshed
    assert calls[0]==("get",("tenant-a","shop-a","employee-a"))
    assert calls[1]==("delete",("tenant-a","shop-a","employee-a"))
    assert calls[2][0]=="save"
