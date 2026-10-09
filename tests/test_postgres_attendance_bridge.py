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
        assert {'effective_predecessor_id','registration_reason'} <= columns
    finally:
        if store: store.engine.dispose()
        with admin.begin() as conn: conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


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
