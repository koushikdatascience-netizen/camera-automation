import hashlib
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import cloud_portal.api as api


def principal():
    return api.EdgePrincipal(
        tenant_id="tenant-1", company_code="2", shop_id="WBTEST",
        site_id="legacy-site", edge_id="edge-1"
    )


def test_scope_guard_accepts_bound_identity():
    api._enforce_edge_scope(principal(), {
        "tenant_id":"tenant-1","company_code":"2","shop_id":"WBTEST",
        "site_id":"legacy-site","edge_id":"edge-1"
    })


@pytest.mark.parametrize(("field","value"), [
    ("tenant_id","tenant-2"),("company_code","3"),("shop_id","OTHER"),
    ("site_id","other-site"),("edge_id","edge-2"),
])
def test_scope_guard_rejects_cross_scope(field, value):
    payload={"tenant_id":"tenant-1","company_code":"2","shop_id":"WBTEST",
             "site_id":"legacy-site","edge_id":"edge-1"}
    payload[field]=value
    with pytest.raises(HTTPException) as exc:
        api._enforce_edge_scope(principal(), payload)
    assert exc.value.status_code == 403


def test_missing_token_is_rejected(monkeypatch):
    with pytest.raises(HTTPException) as exc:
        api.require_edge_token(None)
    assert exc.value.status_code == 401


def test_production_legacy_global_token_is_fail_closed(monkeypatch):
    monkeypatch.setenv("SNAPKEY_ENV","production")
    monkeypatch.setenv("SNAPKEY_EDGE_API_TOKEN","legacy")
    monkeypatch.delenv("SNAPKEY_ALLOW_LEGACY_GLOBAL_EDGE_TOKEN",raising=False)
    monkeypatch.setattr(api.store,"resolve_edge_credential",lambda token_hash: None)
    with pytest.raises(HTTPException) as exc:
        api.require_edge_token("Bearer legacy")
    assert exc.value.status_code == 401


def test_scoped_token_resolves_to_principal(monkeypatch):
    token="edge-secret"
    expected=hashlib.sha256(token.encode()).hexdigest()
    def resolve(value):
        assert value == expected
        return {"tenant_id":"tenant-1","company_code":"2","shop_id":"WBTEST",
                "site_id":"legacy-site","edge_id":"edge-1"}
    monkeypatch.setattr(api.store,"resolve_edge_credential",resolve)
    p=api.require_edge_token("Bearer "+token)
    assert p.shop_id == "WBTEST"
    assert p.edge_id == "edge-1"


def test_heartbeat_scope_guard_rejects_other_shop():
    payload={"tenant_id":"tenant-1","company_code":"2","shop_id":"OTHER",
             "site_id":"legacy-site","edge_id":"edge-1","status":{}}
    with pytest.raises(HTTPException) as exc:
        api._enforce_edge_scope(principal(), payload)
    assert exc.value.status_code == 403


def test_edge_camera_config_is_bound_to_credential_scope(monkeypatch):
    calls = {}
    def list_cameras(tenant_id, shop_id=None, edge_id=None):
        calls.update(tenant_id=tenant_id, shop_id=shop_id, edge_id=edge_id)
        return [{"camera_id":"CAM-1","shop_id":"WBTEST","edge_id":"edge-1"}]
    monkeypatch.setattr(api.store, "list_cameras", list_cameras)
    result = api.edge_camera_config(principal())
    assert calls == {"tenant_id":"tenant-1","shop_id":"WBTEST","edge_id":"edge-1"}
    assert result["items"][0]["camera_id"] == "CAM-1"


def test_edge_camera_config_rejects_legacy_global_credential():
    with pytest.raises(HTTPException) as exc:
        api.edge_camera_config(api.EdgePrincipal(legacy_global=True))
    assert exc.value.status_code == 403
