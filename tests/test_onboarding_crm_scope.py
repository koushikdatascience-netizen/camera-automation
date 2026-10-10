from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cloud_portal import api


def test_activation_grant_uses_consumed_scope_not_request_body(monkeypatch):
    calls = []
    monkeypatch.setenv("SNAPKEY_CRM_INTEGRATION_KEY", "synthetic-key")
    monkeypatch.setattr(api, "store", SimpleNamespace(
        consume_edge_activation_code=lambda *_: {"tenant_id":"trusted-tenant", "shop_id":"trusted-shop", "company_code":"trusted"},
        provision_edge_credential=lambda *a: calls.append(("device", a)),
        set_crm_integration_scope=lambda *a, **kw: calls.append(("scope", a, kw))))
    monkeypatch.setattr(api, "_issue_license_payload", lambda **_: {"license":{}, "signature":"synthetic-signature"})
    api.activate_edge(api.EdgeActivationRequest(machine_code="synthetic-machine",activation_code="one-time",company_code="attacker",shop_code="attacker-shop"))
    assert calls[1][1] == (api._token_digest("synthetic-key"), "trusted-tenant", "trusted-shop")


def test_invalid_activation_never_grants_scope(monkeypatch):
    monkeypatch.setenv("SNAPKEY_ENV", "production")
    monkeypatch.setattr(api, "store", SimpleNamespace(consume_edge_activation_code=lambda *_: None))
    with pytest.raises(HTTPException) as exc:
        api.activate_edge(api.EdgeActivationRequest(machine_code="synthetic-machine",activation_code="invalid",shop_code="shop"))
    assert exc.value.status_code == 401


def test_authenticated_admin_code_generation_binds_only_own_shop(monkeypatch):
    calls = []
    monkeypatch.setenv("SNAPKEY_CRM_INTEGRATION_KEY", "synthetic-key")
    monkeypatch.setattr(api, "store", SimpleNamespace(
        create_edge_activation_code=lambda value: calls.append(("code", value)),
        set_crm_integration_scope=lambda *a, **kw: calls.append(("scope", a))))
    principal = api.PortalPrincipal("s", "tenant", None, "shop", "admin", "Synthetic", "ADMIN")
    api.create_edge_activation_code("tenant", api.EdgeActivationCodeRequest(), principal)
    assert calls[1][1] == (api._token_digest("synthetic-key"), "tenant", "shop")
    before = len(calls)
    with pytest.raises(HTTPException):
        api.create_edge_activation_code("other-tenant", api.EdgeActivationCodeRequest(), principal)
    assert len(calls) == before


def test_non_admin_cannot_provision_crm_scope(monkeypatch):
    monkeypatch.setattr(api, "store", SimpleNamespace())
    principal = api.PortalPrincipal("s", "tenant", None, "shop", "user", "Synthetic", "USER")
    with pytest.raises(HTTPException) as exc:
        api.create_edge_activation_code("tenant", api.EdgeActivationCodeRequest(), principal)
    assert exc.value.status_code == 403
