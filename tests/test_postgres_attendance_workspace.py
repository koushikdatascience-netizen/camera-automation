"""Actual PostgreSQL schemas; all CRM mutations are replaced with explicit mocks."""
import os
import uuid
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from fastapi.testclient import TestClient
from cloud_portal.postgres_storage import PostgresPortalStore


@pytest.fixture
def pg_workspace():
    dsn=os.getenv('SNAPKEY_TEST_DATABASE_URL','')
    if not dsn:
        if os.getenv('SNAPKEY_REQUIRE_POSTGRES_TESTS')=='1':pytest.fail('Isolated PostgreSQL DSN required')
        pytest.skip('Isolated PostgreSQL unavailable')
    url=make_url(dsn)
    if url.host not in {'localhost','127.0.0.1'} or url.database!='camera_eye_test':pytest.fail('Refusing non-isolated test database')
    schema='camera_eye_workspace_'+uuid.uuid4().hex[:12];engine=create_engine(dsn)
    with engine.begin() as conn:conn.execute(text('CREATE SCHEMA "'+schema+'"'))
    scoped=url.update_query_dict({'options':'-csearch_path='+schema+',public'})
    store=PostgresPortalStore(scoped.render_as_string(hide_password=False))
    try:yield store
    finally:
        store.engine.dispose()
        with engine.begin() as conn:conn.execute(text('DROP SCHEMA "'+schema+'" CASCADE'))
        engine.dispose()


def seed(store):
    person=store.create_cloud_person({'id':'person','tenant_id':'tenant','shop_id':'shop','employee_code':'SYN','full_name':'Synthetic Employee','role':'WORKER'})
    store.upsert_crm_person_mapping({'tenant_id':'tenant','shop_id':'shop','local_person_id':person['id'],'crm_user_id':'crm-user','break_master_id':'lunch'})
    return person


def test_pg_workspace_manual_cycle_idempotency_and_session_pairing(pg_workspace,monkeypatch):
    from cloud_portal import api
    seed(pg_workspace);monkeypatch.setattr(api,'store',pg_workspace)
    monkeypatch.setattr(api,'_portal_camera_lookup',lambda *_:{'camera_role':'ENTRANCE_EXIT','site_id':'shop','camera_zone':'inside'})
    calls=[]
    monkeypatch.setattr(api,'_deliver_crm_attendance_event',lambda envelope:calls.append(envelope['event_type']))
    principal=api.PortalPrincipal('test','tenant',None,'shop','operator','Synthetic Operator','ADMIN')
    now=datetime.now(timezone.utc)
    pg_workspace.ingest_event({'event_id':'recognition','tenant_id':'tenant','shop_id':'shop','site_id':'shop','edge_id':'edge','camera_id':'camera',
        'event_type':'PERSON_RECOGNIZED','event_time':now.isoformat(),'payload':{'person_id':'person','metadata':{'confidence':.99}}})
    kwargs={'camera_id':'camera','edge_id':'edge','recognition_event_id':'recognition'}
    results=[]
    for action in ('CHECK_IN','BREAK_START','BREAK_END','CHECK_OUT'):
        request=api.AttendanceStationActionRequest(**kwargs,action=action)
        result=api.attendance_station_action_v2('tenant',request,principal)
        assert result['ok'] is True,result
        results.append(result)
        duplicate=api.attendance_station_action_v2('tenant',request,principal)
        assert duplicate['duplicate'] and duplicate['ok']
    assert calls==['ATTENDANCE_ENTRY','BREAK_START','BREAK_END','ATTENDANCE_EXIT']
    receipts=[pg_workspace.attendance_delivery_receipt('tenant','shop',r['audit_event_id']) for r in results]
    assert len({r['session_id'] for r in receipts})==1
    assert [r['status'] for r in receipts]==['SUCCEEDED']*4
    from camera_service.attendance_workspace_api import cloud_sources
    from camera_service.attendance_workspace import WorkspaceQuery,build_workspace
    people,sessions,events=cloud_sources(pg_workspace,'tenant','shop')
    report=build_workspace(people,sessions,events,WorkspaceQuery(timezone='UTC'))
    assert report['total']==1 and [e['activity'] for e in report['events']]==['LOGIN','BREAK_IN','BREAK_OUT','LOGOUT']
    assert report['items'][0]['status']=='OUT'


def test_cloud_workspace_real_chrome(pg_workspace,monkeypatch):
    from cloud_portal import api
    from attendance_browser_support import verify_workspace_browser
    test_pg_workspace_manual_cycle_idempotency_and_session_pairing(pg_workspace,monkeypatch)
    from fastapi import HTTPException
    def unavailable_preview(*_):raise HTTPException(404,'Synthetic fixture has no enrolled photograph')
    monkeypatch.setattr(api,'_enrollment_preview',unavailable_preview)
    principal=api.PortalPrincipal('test','tenant',None,'shop','operator','Synthetic Operator','ADMIN')
    api.app.dependency_overrides[api.require_portal_session]=lambda:principal
    try:
        verify_workspace_browser(api.app,'/portal/v2/tenants/tenant/attendance','cloud','Synthetic Employee')
    finally:api.app.dependency_overrides.pop(api.require_portal_session,None)


def test_pg_policy_permissions_scope_and_additive_upgrade(pg_workspace,monkeypatch):
    from cloud_portal import api
    seed(pg_workspace);monkeypatch.setattr(api,'store',pg_workspace)
    owner=api.PortalPrincipal('test','tenant',None,'shop','admin','Synthetic','ADMIN')
    manager=api.PortalPrincipal('test','tenant',None,'shop','manager','Synthetic','MANAGER')
    api.app.dependency_overrides[api.require_portal_session]=lambda:owner
    try:
        client=TestClient(api.app)
        value=client.get('/portal/v2/tenants/tenant/attendance/policy').json()['policy']
        value.update(shift_start_time='09:00',scheduled_weekdays=[0,1,2,3,4],late_grace_minutes=10)
        assert client.put('/portal/v2/tenants/tenant/attendance/policy',json=value).status_code==200
        assert pg_workspace.attendance_policy('tenant','shop')['shift_start_time']=='09:00'
        person_policy=client.get('/portal/v2/tenants/tenant/attendance/policy?person_id=person').json()
        person_policy['policy'].update(attendance_mode='MANUAL',absence_monitoring_enabled=False)
        saved=client.put('/portal/v2/tenants/tenant/attendance/policy?person_id=person',json=person_policy['policy'])
        assert saved.status_code==200
        effective=client.get('/portal/v2/tenants/tenant/attendance/policy?person_id=person').json()
        assert effective['policy']['attendance_mode']=='MANUAL'
        assert effective['policy']['absence_monitoring_enabled'] is False
        assert effective['sources']['attendanceMode']=='USER'
        assert client.get('/portal/v2/tenants/other/attendance/workspace').status_code==403
        assert client.get('/portal/v2/tenants/tenant/attendance/workspace?shop_id=other').status_code==403
        api.app.dependency_overrides[api.require_portal_session]=lambda:manager
        assert client.get('/portal/v2/tenants/tenant/attendance/workspace').status_code==200
        assert client.put('/portal/v2/tenants/tenant/attendance/policy',json=value).status_code==403
        restricted=api.PortalPrincipal('test','tenant',None,'shop','user','Synthetic','USER')
        api.app.dependency_overrides[api.require_portal_session]=lambda:restricted
        for path in ('workspace','personnel','policy','events/nonexistent/notifications'):
            assert client.get('/portal/v2/tenants/tenant/attendance/'+path).status_code==403
        with pg_workspace._conn() as conn:conn.execute(text('ALTER TABLE attendance_policies DROP COLUMN workspace_rules_json'))
        pg_workspace._init()
        assert pg_workspace.attendance_policy('tenant','shop')['total_working_minutes']==value['total_working_minutes']
        assert pg_workspace.get_cloud_person('tenant','shop','person')['full_name']=='Synthetic Employee'
    finally:api.app.dependency_overrides.pop(api.require_portal_session,None)


def test_pg_uncertain_crm_result_stays_reviewable_without_duplicate_calls(pg_workspace,monkeypatch):
    from cloud_portal import api
    import httpx
    seed(pg_workspace);monkeypatch.setattr(api,'store',pg_workspace)
    monkeypatch.setattr(api,'_portal_camera_lookup',lambda *_:{'camera_role':'ENTRANCE_EXIT','site_id':'shop'})
    def uncertain(_):
        error=httpx.ReadTimeout('synthetic timeout')
        error.attendance_mutation_attempted=True
        raise error
    monkeypatch.setattr(api,'_deliver_crm_attendance_event',uncertain)
    now=datetime.now(timezone.utc)
    pg_workspace.ingest_event({'event_id':'recognition','tenant_id':'tenant','shop_id':'shop','site_id':'shop','edge_id':'edge','camera_id':'camera',
        'event_type':'PERSON_RECOGNIZED','event_time':now.isoformat(),'payload':{'person_id':'person'}})
    request=api.AttendanceStationActionRequest(camera_id='camera',edge_id='edge',recognition_event_id='recognition',action='CHECK_IN')
    principal=api.PortalPrincipal('test','tenant',None,'shop','operator','Synthetic','ADMIN')
    result=api.attendance_station_action_v2('tenant',request,principal)
    assert not result['ok'] and result['delivery']=='RECONCILIATION_REQUIRED'
    assert api.attendance_station_action_v2('tenant',request,principal)['duplicate']
    assert not pg_workspace.get_person_attendance_presence('tenant','shop','crm-user')


def test_pg_activity_evidence_requires_matching_scoped_recognition(pg_workspace,tmp_path,monkeypatch):
    from cloud_portal import api
    seed(pg_workspace);monkeypatch.setattr(api,'store',pg_workspace)
    monkeypatch.setenv('SNAPKEY_EVIDENCE_ROOT',str(tmp_path))
    folder=tmp_path/'tenant'/'shop'/'edge';folder.mkdir(parents=True)
    image=folder/'test.jpg';image.write_bytes(b'\xff\xd8synthetic')
    evidence={'evidence_id':'tenant/shop/edge/test.jpg'}
    now=datetime.now(timezone.utc)
    pg_workspace.ingest_event({'event_id':'parent','tenant_id':'tenant','shop_id':'shop','site_id':'shop','edge_id':'edge','camera_id':'camera',
        'event_type':'PERSON_RECOGNIZED','event_time':now.isoformat(),'payload':{'person_id':'person','metadata':{'cloud_evidence':evidence}}})
    pg_workspace.record_attendance_activity({'id':'activity','tenant_id':'tenant','shop_id':'shop','crm_user_id':'crm-user','local_person_id':'person',
        'activity_type':'CHECK_IN','occurred_at':now,'source':'MANUAL','camera_id':'camera','metadata':{'recognition_event_id':'parent'}})
    principal=api.PortalPrincipal('test','tenant',None,'shop','operator','Synthetic','ADMIN')
    api.app.dependency_overrides[api.require_portal_session]=lambda:principal
    try:
        client=TestClient(api.app);report=client.get('/portal/v2/tenants/tenant/attendance/workspace?timezone=UTC').json()
        event=report['events'][0]
        assert event['evidence']['event_id']=='activity' and event['evidence']['media_event_id']=='parent'
        url=event['evidence']['images'][0]['url'];assert client.get(url).status_code==200
        assert str(tmp_path) not in str(report)
        with pg_workspace._conn() as conn:conn.execute(text("UPDATE attendance_activity SET camera_id='different' WHERE id='activity'"))
        assert not client.get('/portal/v2/tenants/tenant/attendance/workspace?timezone=UTC').json()['events'][0]['evidence']['images']
    finally:api.app.dependency_overrides.pop(api.require_portal_session,None)


def test_pg_concurrent_portal_logins_create_only_one_session(pg_workspace,monkeypatch):
    from cloud_portal import api
    from fastapi import HTTPException
    from concurrent.futures import ThreadPoolExecutor
    import time
    seed(pg_workspace);monkeypatch.setattr(api,'store',pg_workspace)
    monkeypatch.setattr(api,'_portal_camera_lookup',lambda *_:{'camera_role':'ENTRANCE_EXIT','site_id':'shop'})
    calls=[]
    def deliver(envelope):calls.append(envelope['event_type']);time.sleep(.1)
    monkeypatch.setattr(api,'_deliver_crm_attendance_event',deliver)
    now=datetime.now(timezone.utc)
    pg_workspace.ingest_event({'event_id':'recognition','tenant_id':'tenant','shop_id':'shop','site_id':'shop','edge_id':'edge','camera_id':'camera',
        'event_type':'PERSON_RECOGNIZED','event_time':now.isoformat(),'payload':{'person_id':'person'}})
    principal=api.PortalPrincipal('test','tenant',None,'shop','operator','Synthetic','ADMIN')
    def login(key):
        request=api.AttendanceStationActionRequest(camera_id='camera',edge_id='edge',recognition_event_id='recognition',action='CHECK_IN',request_id=key)
        try:return api.attendance_station_action_v2('tenant',request,principal)['ok']
        except HTTPException as error:assert error.status_code==409;return False
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(login,['first','second']))
    assert sorted(results)==[False,True] and calls==['ATTENDANCE_ENTRY']
