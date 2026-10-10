"""Actual PostgreSQL contracts in a disposable schema of a test-only database."""
import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from cloud_portal.postgres_storage import PostgresPortalStore


def _test_dsn():
    dsn=os.getenv('SNAPKEY_TEST_DATABASE_URL','')
    if not dsn:
        if os.getenv('SNAPKEY_REQUIRE_POSTGRES_TESTS')=='1': pytest.fail('Test PostgreSQL DSN required')
        pytest.skip('Isolated PostgreSQL test DSN not configured')
    if 'test' not in str(make_url(dsn).database or '').lower():
        pytest.fail('Refusing writes outside an explicitly named test database')
    return dsn


@pytest.fixture
def pg_bridge():
    dsn=_test_dsn()
    url=make_url(dsn)
    schema='camera_eye_bridge_test_'+uuid.uuid4().hex[:12]
    admin=create_engine(dsn)
    store=None
    with admin.begin() as conn: conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    try:
        scoped=url.update_query_dict({'options':f'-csearch_path={schema},public'})
        store=PostgresPortalStore(scoped.render_as_string(hide_password=False))
        yield store
    finally:
        if store: store.engine.dispose()
        with admin.begin() as conn: conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_pg_upgrade_adds_predecessor_recovery_columns():
    dsn=_test_dsn(); url=make_url(dsn)
    schema='camera_eye_bridge_upgrade_'+uuid.uuid4().hex[:12]
    admin=create_engine(dsn); store=None
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))
        conn.execute(text(f'''CREATE TABLE "{schema}".edge_attendance_delivery(
            event_id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,shop_id TEXT NOT NULL,
            edge_id TEXT NOT NULL,person_id TEXT NOT NULL,crm_user_id TEXT NOT NULL,
            session_id TEXT NOT NULL,predecessor_id TEXT,identity_hash TEXT NOT NULL,
            event_type TEXT NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
            claimed_at TEXT,next_attempt_at TEXT,error_code TEXT,updated_at TEXT NOT NULL,
            reconciled_by TEXT,reconciliation_note TEXT,reconciled_at TEXT)'''))
    try:
        scoped=url.update_query_dict({'options':f'-csearch_path={schema},public'})
        store=PostgresPortalStore(scoped.render_as_string(hide_password=False))
        with store._conn() as conn:
            columns={row[0] for row in conn.execute(text("""SELECT column_name FROM information_schema.columns
                WHERE table_schema=:schema AND table_name='edge_attendance_delivery'"""),
                {'schema':schema}).all()}
        assert {'effective_predecessor_id','registration_reason','crm_message'} <= columns
    finally:
        if store: store.engine.dispose()
        with admin.begin() as conn: conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_pg_crm_identity_cannot_duplicate_or_reassign(pg_bridge):
    store=pg_bridge
    mapping={'tenant_id':'tenant','shop_id':'shop','local_person_id':'person','crm_user_id':'crm'}
    store.upsert_crm_person_mapping(mapping)
    with pytest.raises(ValueError):store.upsert_crm_person_mapping({**mapping,'crm_user_id':'other'})
    from sqlalchemy.exc import IntegrityError
    with pytest.raises(IntegrityError):store.upsert_crm_person_mapping({**mapping,'local_person_id':'other'})
    assert store.crm_person_mapping('tenant','shop','person')['crm_user_id']=='crm'
    store.upsert_crm_person_mapping({**mapping,'shop_id':'other'})


def test_pg_enrollment_to_edge_recognition_and_authentication(pg_bridge,tmp_path,monkeypatch):
    import base64
    import cv2
    import numpy as np
    from types import SimpleNamespace
    from cloud_portal import api
    from cloud_portal.crm_client import SnapKeyCrmClient
    from camera_service.storage import SQLiteStore
    from camera_service import face_service
    ok,image=cv2.imencode('.jpg',np.zeros((32,32,3),dtype=np.uint8));assert ok
    source=base64.b64encode(image).decode()
    vector=[1.]+[0.]*511
    monkeypatch.setattr(api,'store',pg_bridge)
    monkeypatch.setattr(api,'crm_client',SimpleNamespace(face_embeddings=lambda *a,**kw:[{
        'id':'employee','tenantId':'uuid','tenantCode':'tenant','shopId':'shop','name':'Synthetic',
        'isActive':True,'faceImages':[source]}],business_success=SnapKeyCrmClient.business_success))
    monkeypatch.setattr(api,'enrollment_model_key',lambda:'synthetic-model')
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda:'synthetic-model')
    monkeypatch.setattr(api,'_cloud_face_enroller',lambda:SimpleNamespace(enroll=lambda _: (vector,.9)))
    monkeypatch.setattr(api,'_refresh_crm_personnel',lambda *a,**kw:None)
    api._refresh_crm_personnel_locked('tenant','shop')
    principal=api.EdgePrincipal(tenant_id='tenant',company_code=None,shop_id='shop',site_id='site',edge_id='edge')
    config=api.edge_personnel_config(principal)
    assert config['items'][0]['faces'][0]['model_key']=='synthetic-model'
    local=SQLiteStore(str(tmp_path/'edge.db'));local.configure_event_scope('tenant',None,'shop','edge')
    local.apply_cloud_personnel(config['items'])
    recognizer=face_service.FaceService.__new__(face_service.FaceService);recognizer.store=local
    result,similarity=recognizer.recognize(vector,.5)
    assert similarity==pytest.approx(1)
    assert result['person_id']==config['items'][0]['person_id']
    assert api._crm_face_login_identity('tenant','employee','shop')==('uuid',source)
    assert api._validated_crm_face_login_token({'success':True,'token':'synthetic','user':{'id':'employee','tenantId':'uuid'}},'employee','uuid')=='synthetic'
    assert api._enrollment_preview('tenant','shop',result['person_id']).body.startswith(b'\xff\xd8')
    from cryptography.fernet import Fernet
    monkeypatch.setenv('CAMERA_EYE_TOKEN_ENCRYPTION_KEY',Fernet.generate_key().decode())
    calls=[]
    def login(enrolled,tenant):
        assert enrolled==source and tenant=='uuid'
        calls.append(1)
        return {'success':True,'token':'synthetic','user':{'id':'employee','tenantId':'uuid'}}
    api.crm_client.login_using_face_tenant=login
    # Synthetic opaque credential has no JWT expiry; opt into a short test-only TTL.
    monkeypatch.setenv('SNAPKEY_CRM_TOKEN_FALLBACK_TTL_SECONDS','600')
    assert api._crm_face_token('tenant','shop','employee')=='synthetic'
    assert api._crm_face_token('tenant','shop','employee')=='synthetic' and calls==[1]
    saved=pg_bridge.get_crm_face_token('tenant','shop','employee')
    assert 'synthetic' not in saved['encrypted_token']
    assert pg_bridge.get_crm_face_token('tenant','other','employee') is None


def event(key='entry',kind='ATTENDANCE_ENTRY',predecessor=None,session='session'):
    return {'tenant_id':'tenant','shop_id':'shop','site_id':'site','edge_id':'edge','camera_id':'camera',
        'event_id':key,'event_type':kind,'event_time':datetime.now(timezone.utc).isoformat(),
        'payload':{'person_id':'person','metadata':{'attendance_sync_bridge':True,
            'attendance_source':'MANUAL','attendance_session_id':session,'predecessor_event_id':predecessor}}}


def test_pg_concurrent_claim_duplicate_recovery_and_tenant_isolation(pg_bridge):
    store=pg_bridge; envelope=event()
    store.ingest_event(envelope)
    assert store.ingest_event(envelope)['inserted'] is False
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims=list(pool.map(lambda _:store.claim_attendance_delivery(envelope,'employee'),range(2)))
    assert sum(claimed for _,claimed in claims)==1
    store.set_attendance_delivery(envelope,'CRM_CONFIRMED')
    assert store.claim_attendance_delivery(envelope,'employee')==('CRM_CONFIRMED',True)
    store.set_attendance_delivery(envelope,'SUCCEEDED')
    assert store.claim_attendance_delivery(envelope,'employee')==('SUCCEEDED',False)
    assert store.has_bridge_attendance('tenant','shop','person')
    assert not store.has_bridge_attendance('other','shop','person')
    with pytest.raises(ValueError,match='identity conflict'):
        store.claim_attendance_delivery({**envelope,'tenant_id':'other'},'employee')
    with pytest.raises(ValueError,match='identity conflict'):
        store.claim_attendance_delivery(envelope,'other-employee')


def test_pg_ordering_reentry_and_stale_claim(pg_bridge):
    store=pg_bridge
    entry=event(); checkout=event('exit','ATTENDANCE_EXIT','entry')
    assert store.claim_attendance_delivery(checkout,'employee')==('WAITING_PREDECESSOR',False)
    assert store.claim_attendance_delivery(entry,'employee')[1]
    store.set_attendance_delivery(entry,'SUCCEEDED')
    assert store.claim_attendance_delivery(checkout,'employee')[1]
    store.set_attendance_delivery(checkout,'SUCCEEDED')
    new=event('reentry','ATTENDANCE_ENTRY','exit','new-session')
    assert store.claim_attendance_delivery(new,'employee')[1]
    assert store.claim_attendance_delivery(new,'employee',datetime.now(timezone.utc)+timedelta(minutes=6))==(
        'RECONCILIATION_REQUIRED',False)


def test_pg_repairs_recognition_predecessor_and_quarantines_missing_break_receipt(pg_bridge):
    store=pg_bridge
    recognized=event('recognized','PERSON_RECOGNIZED')
    recognized['payload']['metadata']={'attendance_sync_bridge':True}
    store.ingest_event(recognized)
    entry=event('entry-auto','ATTENDANCE_ENTRY','recognized','auto-session')
    entry['payload']['metadata']['attendance_source']='RECOGNITION'
    store.ingest_event(entry)
    status,claimed=store.claim_attendance_delivery(entry,'employee')
    assert (status,claimed)==('RETRY',True)
    receipt=store.attendance_delivery_receipt('tenant','shop','entry-auto')
    assert receipt['predecessor_id']=='recognized'
    assert receipt['effective_predecessor_id'] is None
    assert receipt['registration_reason']=='IGNORED_NON_ATTENDANCE_PREDECESSOR'
    store.set_attendance_delivery(entry,'SUCCEEDED')

    start=event('manual:start','BREAK_START','entry-auto','auto-session')
    end=event('manual:end','BREAK_END','manual:start','auto-session')
    store.ingest_event(start); store.ingest_event(end)
    assert store.claim_attendance_delivery(end,'employee')==('WAITING_PREDECESSOR',False)
    recovered=store.attendance_delivery_receipt('tenant','shop','manual:start')
    assert recovered['status']=='RECONCILIATION_REQUIRED'
    assert recovered['registration_reason']=='HISTORICAL_RECEIPT_MISSING'
    assert recovered['attempts']==0


def test_pg_bridge_reuses_existing_attendance_services_without_absence_checkout(pg_bridge,monkeypatch):
    from cloud_portal import api
    store=pg_bridge; now=datetime.now(timezone.utc)
    store.upsert_crm_person_mapping({'tenant_id':'tenant','shop_id':'shop','local_person_id':'person',
        'crm_user_id':'employee','break_master_id':'lunch'})
    monkeypatch.setattr(api,'store',store)
    calls=[]
    monkeypatch.setattr(api,'_deliver_crm_attendance_event',lambda e:calls.append(e['event_id']))
    previous=None
    for key,kind in [('entry','ATTENDANCE_ENTRY'),('start','BREAK_START'),('end','BREAK_END'),('exit','ATTENDANCE_EXIT')]:
        envelope=event(key,kind,previous)
        envelope['payload']['metadata']['crm_confirmed_break']=kind.startswith('BREAK')
        store.ingest_event(envelope)
        assert api._synchronize_attendance_bridge(envelope)['status']=='SUCCEEDED'
        assert api._synchronize_attendance_bridge(envelope)['status']=='SUCCEEDED'
        previous=key
    assert calls==['entry','start','end','exit']
    activities=store.list_attendance_activity('tenant','shop','employee',now-timedelta(days=1),now+timedelta(days=1))
    assert len(activities)==4
    assert {a['metadata']['attendance_session_id'] for a in activities}=={'session'}
    assert store.get_person_attendance_presence('tenant','shop','employee')['checked_in'] is False
    # A later bridge session must not qualify for disappearance-only checkout.
    store.upsert_attendance_policy('tenant','shop',{'grace_period_minutes':1,'absence_auto_logout_enabled':True,
        'allowed_break_minutes':30,'total_working_minutes':480,'max_logoff_time':'23:59','timezone':'UTC'})
    store.touch_attendance_presence(tenant_id='tenant',shop_id='shop',local_person_id='person',
        crm_user_id='employee',seen_at=now-timedelta(hours=2),camera_id='camera',
        recognition_event_id='recognition',checked_in=True)
    assert store.claim_due_absence_checkouts(now)==[]

def test_pg_enrollment_singleflight_and_scope_guard(pg_bridge):
    from sqlalchemy.exc import NoResultFound
    store=pg_bridge
    with store.crm_enrollment_lock('tenant-a','shop-a') as first:
        assert first
        with store.crm_enrollment_lock('tenant-a','shop-a') as second: assert not second
        with store.crm_enrollment_lock('tenant-b','shop-a') as other: assert other
    with store.crm_enrollment_lock('tenant-a','shop-a') as released: assert released
    item={'id':'shared','tenant_id':'tenant-a','shop_id':'shop-a','employee_code':'A','full_name':'A','role':'WORKER'}
    store.upsert_crm_cloud_person(item)
    with pytest.raises(NoResultFound):store.upsert_crm_cloud_person({**item,'tenant_id':'tenant-b'})
    assert store.get_cloud_person('tenant-a','shop-a','shared')


def test_logout_reconciliation_blocks_newer_claim_and_max_logoff_keeps_break_policy(pg_bridge):
    now=datetime.now(timezone.utc)
    tenant,shop,user,person="tenant-test","shop-test","user-test","person-test"
    pg_bridge.touch_attendance_presence(tenant_id=tenant,shop_id=shop,local_person_id=person,
        crm_user_id=user,seen_at=now,camera_id="cam",recognition_event_id="recognition-test",checked_in=True)
    pg_bridge.set_attendance_presence_break(tenant,shop,person,True)
    assert not pg_bridge.claim_crm_auto_logout(tenant,shop,user,now)
    assert pg_bridge.claim_crm_auto_logout(tenant,shop,user,now,allow_on_break=True)
    pg_bridge.mark_crm_auto_logout_reconciliation_required(tenant,shop,user,now,"uncertain upstream result")
    later=now+timedelta(seconds=10)
    pg_bridge.touch_attendance_presence(tenant_id=tenant,shop_id=shop,local_person_id=person,
        crm_user_id=user,seen_at=later,camera_id="cam",recognition_event_id="recognition-test",checked_in=True)
    assert not pg_bridge.claim_crm_auto_logout(tenant,shop,user,later,allow_on_break=True)
