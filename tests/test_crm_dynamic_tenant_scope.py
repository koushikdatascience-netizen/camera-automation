"""Regression coverage for dynamic CRM multi-tenancy without static env IDs."""

from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from cloud_portal import api


def _request(key: str) -> Request:
    return Request({"type": "http", "method": "GET", "path": "/integration/v2/test",
                    "headers": [(b"x-crm-integration-key", key.encode())]})


def test_authenticated_crm_can_use_multiple_dynamic_tenants(monkeypatch):
    monkeypatch.setenv("SNAPKEY_ENV", "production")
    monkeypatch.setenv("SNAPKEY_CRM_INTEGRATION_KEY", "server-secret")
    monkeypatch.delenv("SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES", raising=False)
    for tenant, shop in [("tenant-one", "shop-a"), ("tenant-two", "shop-b")]:
        assert api._require_crm_integration(_request("server-secret"), tenant, shop) is None


def test_untrusted_crm_request_cannot_choose_any_tenant(monkeypatch):
    monkeypatch.setenv("SNAPKEY_ENV", "production")
    monkeypatch.setenv("SNAPKEY_CRM_INTEGRATION_KEY", "server-secret")
    with pytest.raises(HTTPException) as exc:
        api._require_crm_integration(_request("wrong-key"), "tenant-one", "shop-a")
    assert exc.value.status_code == 401


def test_crm_scope_identifiers_cannot_be_empty(monkeypatch):
    monkeypatch.setenv("SNAPKEY_CRM_INTEGRATION_KEY", "server-secret")
    with pytest.raises(HTTPException) as exc:
        api._require_crm_integration(_request("server-secret"), " ", "shop-a")
    assert exc.value.status_code == 400


def test_cloud_compose_does_not_require_static_tenant_ids():
    compose = (Path(__file__).resolve().parents[1] / "docker-compose.cloud.yml").read_text()
    assert "SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES" not in compose
    assert "SNAPKEY_CRM_INTEGRATION_KEY" in compose
