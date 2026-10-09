"""Alert operator actions preserve detector history and remain shop-scoped."""
import json
import os
import uuid
from datetime import datetime,timezone
from types import SimpleNamespace
from fastapi.testclient import TestClient
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
import pytest
from camera_service.storage import SQLiteStore
from camera_service.models import PersonnelCreate,PersonnelRole
from cloud_portal import api
from cloud_portal.api import PortalPrincipal


@pytest.fixture
def pg_workspace():
    dsn=os.getenv('SNAPKEY_TEST_DATABASE_URL','')
    if not dsn:
        if os.getenv('SNAPKEY_REQUIRE_POSTGRES_TESTS')=='1':pytest.fail('Isolated PostgreSQL DSN required')
        pytest.skip('PostgreSQL test database unavailable')
    url=make_url(dsn)
    if url.host not in {'127.0.0.1','localhost'} or url.database!='camera_eye_test':pytest.fail('Refusing non-isolated PostgreSQL database')
    engine=create_engine(dsn);schema='camera_eye_alerts_'+uuid.uuid4().hex[:12]
    with engine.begin() as conn:conn.execute(text('CREATE SCHEMA "'+schema+'"'))
    store_url=url.update_query_dict({'options':'-csearch_path='+schema+',public'})
    from cloud_portal.postgres_storage import PostgresPortalStore
    store=PostgresPortalStore(store_url.render_as_string(hide_password=False))
    try:yield store
    finally:
        store.engine.dispose()
        with engine.begin() as conn:conn.execute(text('DROP SCHEMA "'+schema+'" CASCADE'))
        engine.dispose()


def test_local_alert_filters_detail_ack_resolution_history_and_export(tmp_path,monkeypatch):
    store=SQLiteStore(str(tmp_path/'alerts.db'))
    stamp=datetime.now(timezone.utc).isoformat()
    with store._conn() as conn:
        conn.execute("INSERT INTO unknown_incidents(id,store_id,camera_id,track_id,first_seen,confirmed_unknown_at,last_seen,recognition_attempts,status) VALUES(?,?,?,?,?,?,?,?,?)",
            ('unknown1','shop','camera-a','track','2026-10-10T10:00:00+00:00',stamp,stamp,4,'OPEN'))
        conn.execute("INSERT INTO security_alerts(id,store_id,camera_id,alert_type,object_label,confidence,event_time,status,metadata_json) VALUES(?,?,?,?,?,?,?,?,?)",
            ('security1','other-shop','camera-b','SHOPLIFTING_ALERT','bag',.95,stamp,'OPEN','{}'))
    monkeypatch.setattr('camera_service.api.store',store)
    monkeypatch.setattr('camera_service.api.config',SimpleNamespace(store_id='shop',evidence_dir=str(tmp_path)))
    client=TestClient(__import__('camera_service.api',fromlist=['app']).app)
    response=client.get('/api/v2/alerts/workspace?timezone=UTC')
    assert response.status_code==200 and response.json()['total']==1
    alert=response.json()['items'][0]
    assert alert['person_id'] is None and alert['employee_name'] is None and alert['type']=='UNKNOWN_INCIDENT'
    assert client.get('/api/v2/alerts/workspace?shop_id=other-shop').status_code==403
    assert client.get('/api/v2/alerts/workspace?status=BOGUS').status_code==422
    assert client.get('/api/v2/alerts/export?timezone=UTC').content.startswith(b'\xef\xbb\xbf')
    body={'action':'ACKNOWLEDGE','expected_status':'OPEN','request_id':'ack-1','reason':'Reviewed by operator'}
    action=client.post('/api/v2/alerts/'+alert['id']+'/action',json=body).json()
    assert action['status']=='ACKNOWLEDGED' and client.post('/api/v2/alerts/'+alert['id']+'/action',json=body).json()['duplicate']
    detail=client.get('/api/v2/alerts/'+alert['id']).json()
    assert detail['history'][0]['reason']=='Reviewed by operator' and detail['history'][0]['actor']=='LOCAL_OPERATOR'
    assert client.post('/api/v2/alerts/'+alert['id']+'/action',json={**body,'action':'RESOLVE','expected_status':'ACKNOWLEDGED','request_id':'resolve-1','reason':'Follow-up complete'}).json()['status']=='RESOLVED'
    assert client.post('/api/v2/alerts/'+alert['id']+'/action',json={**body,'request_id':'ack-2'}).status_code==409


def test_local_policy_mode_is_effective_with_employee_precedence(tmp_path,monkeypatch):
    store=SQLiteStore(str(tmp_path/'policy.db'))
    employee=store.create_person(PersonnelCreate(employee_code='POL',full_name='Policy Person',role=PersonnelRole.WORKER))
    person_id=employee['id']
    with store._conn() as conn:
        conn.execute("INSERT INTO attendance_workspace_policies VALUES(?,?,?,?)",('shop','',json.dumps({'attendance_mode':'AUTO'}),store.now()))
        conn.execute("INSERT INTO attendance_workspace_policies VALUES(?,?,?,?)",('shop',person_id,json.dumps({'attendance_mode':'MANUAL'}),store.now()))
    assert store.effective_attendance_mode(person_id,'shop','AUTO')=='MANUAL'
    with store._conn() as conn:conn.execute('DELETE FROM attendance_workspace_policies WHERE store_id=? AND person_id=?',('shop',person_id))
    assert store.effective_attendance_mode(person_id,'shop','MANUAL')=='AUTO'
    from camera_service import api as local_api
    monkeypatch.setattr(local_api,'store',store)
    monkeypatch.setattr(local_api,'config',SimpleNamespace(store_id='shop',evidence_dir=str(tmp_path)))
    client=TestClient(local_api.app)
    with store._conn() as conn:
        conn.execute('UPDATE attendance_workspace_policies SET policy_json=? WHERE store_id=? AND person_id=?',(json.dumps({'grace_period_minutes':12}),'shop',''))
        conn.execute("INSERT INTO attendance_workspace_policies VALUES(?,?,?,?)",('shop',person_id,json.dumps({'attendance_mode':'MANUAL'}),store.now()))
    result=client.get('/api/v2/attendance/policy?person_id='+person_id).json()
    assert result['sources']['attendance_mode']=='EMPLOYEE'
    assert result['sources']['grace_period_minutes']=='SHOP'


def test_cloud_alert_ack_resolution_persists_and_enforces_tenant_shop(pg_workspace,monkeypatch):
    from camera_service.alerts_workspace import initialize
    initialize(pg_workspace);monkeypatch.setattr(api,'store',pg_workspace)
    stamp=datetime.now(timezone.utc).isoformat()
    pg_workspace.ingest_event({'event_id':'offline','tenant_id':'tenant','shop_id':'shop','site_id':'shop','edge_id':'edge','camera_id':'cam',
        'event_type':'CAMERA_OFFLINE','event_time':stamp,'payload':{'metadata':{'reason':'heartbeat timeout'}}})
    owner=PortalPrincipal('session','tenant',None,'shop','admin','Operator','ADMIN')
    api.app.dependency_overrides[api.require_portal_session]=lambda:owner
    try:
        client=TestClient(api.app);response=client.get('/portal/v2/tenants/tenant/alerts/workspace?timezone=UTC')
        assert response.status_code==200 and response.json()['total']==1
        item=response.json()['items'][0];assert item['type']=='CAMERA_OFFLINE'
        assert item['person_id'] is None and item['severity']=='WARNING'
        assert client.get('/portal/v2/tenants/other/alerts/workspace').status_code==403
        assert client.get('/portal/v2/tenants/tenant/alerts/workspace?shop_id=other').status_code==403
        manager=PortalPrincipal('session','tenant',None,'shop','manager','Manager','MANAGER')
        api.app.dependency_overrides[api.require_portal_session]=lambda:manager
        body={'action':'ACKNOWLEDGE','expected_status':'OPEN','request_id':'ack','reason':'Camera outage assigned'}
        assert client.post('/portal/v2/tenants/tenant/alerts/'+item['id']+'/action',json=body).status_code==200
        assert client.get('/portal/v2/tenants/tenant/alerts/'+item['id']).json()['history'][0]['actor']=='manager'
        regular=PortalPrincipal('session','tenant',None,'shop','worker','Worker','USER')
        api.app.dependency_overrides[api.require_portal_session]=lambda:regular
        assert client.get('/portal/v2/tenants/tenant/alerts/workspace').status_code==403
        assert client.post('/portal/v2/tenants/tenant/alerts/'+item['id']+'/action',json={**body,'request_id':'bad'}).status_code==403
    finally:api.app.dependency_overrides.pop(api.require_portal_session,None)


def test_local_alerts_real_chrome(tmp_path,monkeypatch):
    import cv2,numpy as np
    from alerts_browser_support import verify_alerts_browser
    store=SQLiteStore(str(tmp_path/'alerts-browser.db'));evidence=tmp_path/'evidence';evidence.mkdir()
    image=evidence/'alert.jpg';assert cv2.imwrite(str(image),np.full((100,140,3),90,dtype=np.uint8))
    clip=evidence/'alert.webm';writer=cv2.VideoWriter(str(clip),cv2.VideoWriter_fourcc(*'VP80'),10,(140,100));assert writer.isOpened()
    for n in range(18):writer.write(np.full((100,140,3),30+n*5,dtype=np.uint8))
    writer.release()
    stamp=datetime.now(timezone.utc).isoformat()
    with store._conn() as conn:conn.execute("INSERT INTO security_alerts(id,store_id,camera_id,alert_type,object_label,confidence,event_time,snapshot_path,clip_path,status,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",('shoplift','shop','camera','SHOPLIFTING_ALERT','bag',.95,stamp,str(image),str(clip),'OPEN','{}'))
    import camera_service.api as local_api
    monkeypatch.setattr(local_api,'store',store);monkeypatch.setattr(local_api,'config',SimpleNamespace(store_id='shop',evidence_dir=str(evidence)))
    verify_alerts_browser(local_api.app,'/api/v2/alerts','local','SHOPLIFTING_ALERT')


def test_cloud_alerts_real_chrome(pg_workspace,monkeypatch):
    from alerts_browser_support import verify_alerts_browser
    from camera_service.alerts_workspace import initialize
    initialize(pg_workspace);monkeypatch.setattr(api,'store',pg_workspace)
    pg_workspace.ingest_event({'event_id':'browser-offline','tenant_id':'tenant','shop_id':'shop','site_id':'shop','edge_id':'edge','camera_id':'browser-camera',
        'event_type':'CAMERA_OFFLINE','event_time':datetime.now(timezone.utc).isoformat(),'payload':{'metadata':{'reason':'synthetic browser incident'}}})
    principal=PortalPrincipal('session','tenant',None,'shop','manager','Manager','MANAGER')
    api.app.dependency_overrides[api.require_portal_session]=lambda:principal
    try:verify_alerts_browser(api.app,'/portal/v2/tenants/tenant/alerts','cloud','CAMERA_OFFLINE')
    finally:api.app.dependency_overrides.pop(api.require_portal_session,None)
