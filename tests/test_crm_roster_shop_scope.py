from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from cloud_portal import api


def test_monthly_roster_is_filtered_to_active_current_shop_mappings(monkeypatch):
    crm_rows=[{"userId":"crm-a","attendance":[]},
              {"userId":"crm-b","attendance":[]},
              {"userId":"crm-unmapped","attendance":[]}]
    store=SimpleNamespace(list_crm_person_mappings=lambda tenant,shop:[
        {"crm_user_id":"crm-a","enabled":True},
        {"crm_user_id":"crm-disabled","enabled":False},
    ])
    crm=SimpleNamespace(configured_for_tenant=lambda tenant:True,
                        users_roster=lambda *args,**kwargs:crm_rows)
    monkeypatch.setattr(api,"store",store)
    monkeypatch.setattr(api,"crm_client",crm)
    monkeypatch.setattr(api,"_assert_crm_service_token_scope",lambda *_:None)
    monkeypatch.setattr(api,"_crm_allowed_user_ids",lambda _tenant:{"crm-a","crm-b","crm-unmapped"})
    principal=api.PortalPrincipal("session","tenant",None,"shop-a","user","Operator","ADMIN")

    result=api.crm_attendance_roster("tenant",2026,10,principal=principal)

    assert [row["userId"] for row in result]==["crm-a"]


def test_monthly_roster_rejects_request_for_user_mapped_to_another_shop(monkeypatch):
    store=SimpleNamespace(list_crm_person_mappings=lambda tenant,shop:[
        {"crm_user_id":"crm-a","enabled":True},
    ])
    crm=SimpleNamespace(configured_for_tenant=lambda tenant:True,
                        users_roster=lambda *args,**kwargs:pytest.fail("unexpected roster fetch"))
    monkeypatch.setattr(api,"store",store)
    monkeypatch.setattr(api,"crm_client",crm)
    monkeypatch.setattr(api,"_assert_crm_service_token_scope",lambda *_:None)
    monkeypatch.setattr(api,"_crm_allowed_user_ids",lambda _tenant:{"crm-a","crm-b"})
    principal=api.PortalPrincipal("session","tenant",None,"shop-a","user","Operator","ADMIN")

    with pytest.raises(HTTPException) as exc:
        api.crm_attendance_roster("tenant",2026,10,"crm-b",principal)

    assert exc.value.status_code==404
    assert exc.value.detail=="CRM user is not mapped to this shop"
