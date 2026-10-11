"""Policy delivery tests use SQLite/temp credentials and never call CRM attendance APIs."""
from types import SimpleNamespace
import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from camera_service.attendance_workspace_api import WorkspacePolicy, install_local, local_policy
from camera_service.storage import SQLiteStore
from camera_service.sync_worker import EdgeSyncWorker


def policy(**changes):
    return {**WorkspacePolicy().model_dump(), **changes}


def snapshot(revision=1, **changes):
    value={
        "enabled":True,"tenant_id":"tenant-a","shop_id":"shop-a","edge_id":"edge-a",
        "revision":revision,"updated_at":"2026-10-11T10:00:00+05:30",
        "shop_policy":policy(),"employees":[],
    }
    value.update(changes)
    return value


def add_crm_person(store, person_id="person-a", crm_user_id="crm-a", tenant_id="tenant-a", shop_id="shop-a"):
    now=store.now()
    with store._conn() as conn:
        conn.execute("""INSERT INTO personnel(id,employee_code,full_name,role,active,created_at,updated_at,managed_source,crm_sync_json)
            VALUES(?,?,?,?,1,?,?,?,?)""",(person_id,"EMP-1","Synthetic Person","WORKER",now,now,"crm",
            json.dumps({"crm_user_id":crm_user_id,"tenant_id":tenant_id,"shop_id":shop_id})))


@pytest.fixture
def edge(tmp_path):
    store=SQLiteStore(str(tmp_path/"edge.sqlite"))
    store.configure_event_scope("tenant-a","company-a","shop-a","edge-a")
    return store


def test_cloud_snapshot_becomes_effective_without_overwriting_local_fallback(edge):
    add_crm_person(edge)
    local=policy(total_working_minutes=480,attendance_mode="MANUAL")
    with edge._conn() as conn:
        conn.execute("INSERT INTO attendance_workspace_policies(store_id,person_id,policy_json,updated_at) VALUES(?,?,?,?)",
            ("shop-a","",json.dumps(local),edge.now()))
    cloud= snapshot(4,shop_policy=policy(total_working_minutes=540,attendance_mode="AUTO"),employees=[{
        "local_person_id":"person-a","crm_user_id":"crm-a",
        "policy":policy(total_working_minutes=510,attendance_mode="MANUAL"),
    }])
    assert edge.apply_cloud_attendance_policy(cloud,"tenant-a","shop-a","edge-a")=={
        "status":"APPLIED","revision":4,"employee_count":1}
    assert local_policy(edge,"shop-a")["total_working_minutes"]==540
    assert local_policy(edge,"shop-a","person-a")["total_working_minutes"]==510
    assert edge.effective_attendance_mode("person-a","shop-a")=="MANUAL"
    assert edge.attendance_policy_snapshot("person-a","shop-a")["values"]["requiredWorkingMinutes"]==510
    with edge._conn() as conn:
        saved=json.loads(conn.execute("SELECT policy_json FROM attendance_workspace_policies WHERE store_id='shop-a' AND person_id='' ").fetchone()[0])
    assert saved==local  # Cloud sync never overwrites the local fallback.
    restarted=SQLiteStore(edge.path)
    assert restarted.cloud_attendance_policy_sync_state('shop-a')['revision']==4
    assert local_policy(restarted,'shop-a','person-a')['total_working_minutes']==510


def test_cloud_snapshot_is_idempotent_rejects_stale_and_same_revision_conflict(edge):
    first=snapshot(5)
    assert edge.apply_cloud_attendance_policy(first,"tenant-a","shop-a","edge-a")["status"]=="APPLIED"
    assert edge.apply_cloud_attendance_policy(first,"tenant-a","shop-a","edge-a")["status"]=="DUPLICATE"
    older=snapshot(4,shop_policy=policy(total_working_minutes=600))
    assert edge.apply_cloud_attendance_policy(older,"tenant-a","shop-a","edge-a")=={"status":"STALE","revision":5}
    conflict=snapshot(5,shop_policy=policy(total_working_minutes=600))
    with pytest.raises(ValueError,match="without a newer revision"):
        edge.apply_cloud_attendance_policy(conflict,"tenant-a","shop-a","edge-a")
    assert local_policy(edge,"shop-a")["total_working_minutes"]==540


@pytest.mark.parametrize("field,value",[
    ("tenant_id","tenant-b"),("shop_id","shop-b"),("edge_id","edge-b"),
    ("revision",-1),("updated_at","not-a-time"),
    ("shop_policy",{"total_working_minutes":540}),
])
def test_invalid_scope_revision_timestamp_or_partial_policy_is_rejected_atomically(edge,field,value):
    good=snapshot(2)
    edge.apply_cloud_attendance_policy(good,"tenant-a","shop-a","edge-a")
    bad={**snapshot(3),field:value}
    with pytest.raises(ValueError):edge.apply_cloud_attendance_policy(bad,"tenant-a","shop-a","edge-a")
    state=edge.cloud_attendance_policy_sync_state("shop-a")
    assert state["revision"]==2 and local_policy(edge,"shop-a")["total_working_minutes"]==540


def test_employee_identity_mismatch_and_malformed_override_preserve_previous_snapshot(edge):
    add_crm_person(edge)
    edge.apply_cloud_attendance_policy(snapshot(1),"tenant-a","shop-a","edge-a")
    bad_mapping=snapshot(2,employees=[{"local_person_id":"person-a","crm_user_id":"somebody-else","policy":policy()}])
    with pytest.raises(ValueError,match="CRM mapping"):
        edge.apply_cloud_attendance_policy(bad_mapping,"tenant-a","shop-a","edge-a")
    malformed=snapshot(2,employees=[{"local_person_id":"person-a","crm_user_id":"crm-a","policy":{"attendance_mode":"AUTO"}}])
    with pytest.raises(ValueError,match="incomplete"):
        edge.apply_cloud_attendance_policy(malformed,"tenant-a","shop-a","edge-a")
    assert edge.cloud_attendance_policy_sync_state("shop-a")["revision"]==1


def test_removed_employee_override_is_replaced_by_shop_inheritance(edge):
    add_crm_person(edge)
    edge.apply_cloud_attendance_policy(snapshot(1,shop_policy=policy(total_working_minutes=540),employees=[{
        "local_person_id":"person-a","crm_user_id":"crm-a","policy":policy(total_working_minutes=480),
    }]),"tenant-a","shop-a","edge-a")
    assert local_policy(edge,"shop-a","person-a")["total_working_minutes"]==480
    edge.apply_cloud_attendance_policy(snapshot(2,shop_policy=policy(total_working_minutes=570),employees=[{
        "local_person_id":"person-a","crm_user_id":"crm-a","policy":policy(total_working_minutes=570),
    }]),"tenant-a","shop-a","edge-a")
    assert local_policy(edge,"shop-a","person-a")["total_working_minutes"]==570
    edge.apply_cloud_attendance_policy(snapshot(3,shop_policy=policy(total_working_minutes=600),employees=[]),
        "tenant-a","shop-a","edge-a")
    assert local_policy(edge,"shop-a","person-a")["total_working_minutes"]==600
    with edge._conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM cloud_attendance_policies WHERE store_id='shop-a' AND person_id='person-a'").fetchone()[0]==0


def test_explicit_disable_reverts_to_retained_local_policy_and_reenable_is_idempotent(edge):
    add_crm_person(edge)
    local=policy(total_working_minutes=480)
    with edge._conn() as conn:conn.execute("INSERT INTO attendance_workspace_policies VALUES(?,?,?,?)",("shop-a","",json.dumps(local),edge.now()))
    cloud=snapshot(1,shop_policy=policy(total_working_minutes=600))
    edge.apply_cloud_attendance_policy(cloud,"tenant-a","shop-a","edge-a")
    assert local_policy(edge,"shop-a")["total_working_minutes"]==600
    edge.disable_cloud_attendance_policy_sync("shop-a")
    assert local_policy(edge,"shop-a")["total_working_minutes"]==480
    assert edge.cloud_attendance_policy_sync_state("shop-a")["active"]==0
    assert edge.apply_cloud_attendance_policy(cloud,"tenant-a","shop-a","edge-a")["status"]=="DUPLICATE"
    assert local_policy(edge,"shop-a")["total_working_minutes"]==600


def test_local_api_reports_cloud_revision_and_refuses_conflicting_operator_save(edge):
    edge.apply_cloud_attendance_policy(snapshot(3),"tenant-a","shop-a","edge-a")
    app=FastAPI();install_local(app,lambda:edge,lambda:SimpleNamespace(store_id="shop-a",evidence_dir="."))
    client=TestClient(app)
    result=client.get("/api/v2/attendance/policy").json()
    assert result["scope"]=="CLOUD_SYNCED" and result["revision"]==3 and not result["can_edit"]
    changed=policy(total_working_minutes=480)
    assert client.put("/api/v2/attendance/policy",json=changed).status_code==409
    assert local_policy(edge,"shop-a")["total_working_minutes"]==540


def test_worker_pulls_on_normal_sync_and_keeps_last_snapshot_on_network_failure(edge):
    class Cloud:
        current=snapshot(1,shop_policy=policy(total_working_minutes=525))
        def enabled(self):return True
        def heartbeat(self,*_):return {}
        def edge_commands(self):return []
        def personnel_config(self):return {"items":[{"person_id":"person-a","crm_user_id":"crm-a",
            "tenant_id":"tenant-a","shop_id":"shop-a","employee_code":"EMP-1",
            "full_name":"Synthetic Person","role":"WORKER","active":True,"faces":[]}]}
        def attendance_policy_config(self):
            if isinstance(self.current,Exception):raise self.current
            return self.current
    class License:
        active=True;plan="test";mode="test"
        @staticmethod
        def status():return License()
        @staticmethod
        def allows_feature(name):return name=="cloud_sync"
    cloud=Cloud()
    worker=EdgeSyncWorker(edge,cloud,SimpleNamespace(tenant_id="tenant-a",shop_id="shop-a",edge_id="edge-a",site_id="shop-a",company_code="company-a"),
        SimpleNamespace(batch_size=10),License())
    worker.run_once()
    assert worker.last_result["policy_sync"]["status"]=="APPLIED"
    assert local_policy(edge,"shop-a")["total_working_minutes"]==525
    cloud.current=TimeoutError("network unavailable")
    worker.run_once()
    assert worker.last_result["policy_sync"]["status"]=="FAILED"
    assert local_policy(edge,"shop-a")["total_working_minutes"]==525
    cloud.current=snapshot(2,shop_policy=policy(total_working_minutes=570))
    worker.run_once()
    assert worker.last_result["policy_sync"]["status"]=="APPLIED"
    assert local_policy(edge,"shop-a")["total_working_minutes"]==570
    cloud.current={"enabled":False}
    worker.run_once()
    assert worker.last_result["policy_sync"]["status"]=="DISABLED"
    assert edge.cloud_attendance_policy_sync_state("shop-a")["active"]==0


def test_cloud_fetch_requires_scoped_edge_auth_and_returns_only_bound_shop(monkeypatch):
    from cloud_portal import api
    from cloud_portal.api import EdgePrincipal
    class ScopedStore:
        def attendance_policy_sync_revision(self,tenant,shop):
            return {"revision":2,"updated_at":"2026-10-11T10:00:00+00:00"}
        def attendance_policy(self,tenant,shop):
            return {"grace_period_minutes":15,"allowed_break_minutes":60,
                "total_working_minutes":540,"timezone":"Asia/Kolkata"}
        def list_crm_person_mappings(self,tenant,shop):
            if (tenant,shop)==("tenant-a","shop-a"):
                return [{"local_person_id":"person-a","crm_user_id":"crm-a"}]
            return []
        def get_cloud_person(self,tenant,shop,person):
            return {"id":person,"active":True} if (tenant,shop,person)==("tenant-a","shop-a","person-a") else None
        def person_attendance_policy(self,*_):return {"attendanceMode":"MANUAL"}
    monkeypatch.setattr(api,"store",ScopedStore())
    monkeypatch.setenv("CAMERA_EYE_CLOUD_POLICY_SYNC_ENABLED","1")
    api.app.dependency_overrides[api.require_edge_token]=lambda:EdgePrincipal(
        tenant_id="tenant-a",shop_id="shop-a",site_id="shop-a",edge_id="edge-a")
    try:
        response=TestClient(api.app).get('/edge/v1/config/attendance-policy')
        assert response.status_code==200
        body=response.json()
        assert (body['tenant_id'],body['shop_id'],body['edge_id'])==('tenant-a','shop-a','edge-a')
        assert body['employees'][0]['crm_user_id']=='crm-a'
        api.app.dependency_overrides[api.require_edge_token]=lambda:EdgePrincipal(
            tenant_id="tenant-b",shop_id="shop-b",site_id="shop-b",edge_id="edge-b")
        isolated=TestClient(api.app).get('/edge/v1/config/attendance-policy').json()
        assert isolated['employees']==[] and isolated['tenant_id']=='tenant-b'
        monkeypatch.setenv('CAMERA_EYE_CLOUD_POLICY_SYNC_ENABLED','0')
        assert TestClient(api.app).get('/edge/v1/config/attendance-policy').json()=={'enabled':False}
    finally:
        api.app.dependency_overrides.pop(api.require_edge_token,None)
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as error:api.require_edge_token(None)
    assert error.value.status_code==401
