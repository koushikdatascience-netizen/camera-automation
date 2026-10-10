"""Release-critical transitions, using test identities and no live CRM calls."""
import json
import os
import uuid
from datetime import datetime,timedelta,timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine,text
from sqlalchemy.engine import make_url
from camera_service.storage import SQLiteStore
from camera_service.models import PersonnelCreate,PersonnelRole,IdentitySeen
from camera_service.attendance_engine import AttendanceEngine
from camera_service.attendance_station import AttendanceStation
from camera_service.sync_worker import EdgeSyncWorker
from camera_service.attendance_workspace import WorkspaceQuery,apply_day_decisions
from cloud_portal.postgres_storage import PostgresPortalStore
from cloud_portal import api

UTC=timezone.utc

@pytest.fixture
def pg_release():
    dsn=os.getenv('SNAPKEY_TEST_DATABASE_URL','')
    if not dsn:pytest.skip('Isolated PostgreSQL required')
    url=make_url(dsn)
    assert 'test' in url.database.lower()
    schema='release_test_'+uuid.uuid4().hex[:12];admin=create_engine(dsn)
    with admin.begin() as conn:conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    store=PostgresPortalStore(url.update_query_dict({'options':f'-csearch_path={schema},public'}).render_as_string(hide_password=False))
    try:yield store
    finally:
        store.engine.dispose()
        with admin.begin() as conn:conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()

@pytest.fixture
def edge(tmp_path):
    store=SQLiteStore(str(tmp_path/'edge.db'));store.configure_event_scope('tenant','company','shop','edge')
    person=store.create_person(PersonnelCreate(employee_code='TEST',full_name='Synthetic',role=PersonnelRole.WORKER))
    engine=AttendanceEngine(store,'shop');now=datetime.now(UTC)
    ev=IdentitySeen(store_id='shop',camera_id='cam',track_id='track',person_id=person['id'],timestamp=now,confidence=.95,bbox=(0,0,10,10))
    engine.on_identity(ev)
    return SimpleNamespace(store=store,engine=engine,person=person['id'],now=now,ev=ev)

def request_for(edge):
    session=edge.store.open_session(edge.person,'shop')
    return {'tenant_id':'tenant','edge_id':'edge','absence_started_at':edge.now.isoformat(),'business_date':edge.now.date().isoformat(),
        'event':{'event_id':'cloud-policy-exit','person_id':edge.person,'store_id':'shop','camera_id':'cam',
            'event_type':'ATTENDANCE_EXIT','event_time':(edge.now+timedelta(hours=1)).isoformat(),
            'metadata':{'attendance_session_id':session['id'],'attendance_sync_bridge':True,'attendance_source':'RECOGNITION',
                'predecessor_event_id':session['id'],'reason_code':'ABSENCE_60_MIN_AUTO_LOGOUT','crm_delivery_owner':'cloud'}}}

def test_edge_checkout_is_atomic_idempotent_and_persists_hold(edge):
    request=request_for(edge)
    assert edge.store.apply_policy_checkout(request)['closed']
    assert edge.store.apply_policy_checkout(request)['duplicate']
    restarted=SQLiteStore(edge.store.path)
    assert restarted.open_session(edge.person,'shop') is None
    assert len([e for e in restarted.queued_events() if e['event_type']=='ATTENDANCE_EXIT'])==1
    edge.ev.timestamp+=timedelta(minutes=61);edge.engine.on_identity(edge.ev)
    assert restarted.open_session(edge.person,'shop') is None

def test_policy_checkout_preserves_new_recognition_and_wrong_session(edge):
    request=request_for(edge)
    edge.store.record_attendance_observation(edge.person,'shop',edge.now+timedelta(seconds=1))
    assert edge.store.apply_policy_checkout(request)['error']=='RECOGNITION_ADVANCED'
    request['event']['metadata']['attendance_session_id']='other'
    assert edge.store.apply_policy_checkout(request)['error']=='SESSION_CHANGED'
    assert edge.store.open_session(edge.person,'shop')

def test_manual_logout_cannot_be_undone_but_explicit_checkin_can_resume(edge):
    station=AttendanceStation(edge.engine);candidate=station.candidate('cam',edge.now)
    station.apply('cam',edge.person,candidate['token'],'CHECK_OUT',candidate['state'],'logout',edge.now)
    edge.ev.timestamp+=timedelta(seconds=1);edge.engine.on_identity(edge.ev)
    assert edge.store.open_session(edge.person,'shop') is None
    candidate=station.candidate('cam',edge.ev.timestamp)
    station.apply('cam',edge.person,candidate['token'],'CHECK_IN',candidate['state'],'resume',edge.ev.timestamp)
    assert edge.store.open_session(edge.person,'shop')['entry_confirmed']

def test_worker_rejects_cross_tenant_checkout(edge):
    worker=EdgeSyncWorker(edge.store,None,SimpleNamespace(tenant_id='other',shop_id='shop',edge_id='edge'),None,None)
    with pytest.raises(ValueError,match='scope mismatch'):
        worker._execute_command({'command_type':'ATTENDANCE_POLICY_CHECKOUT','request':request_for(edge)})

def test_confirmed_cloud_manual_cycle_is_mirrored_once(edge):
    # Manual recognition creates an unconfirmed draft, never a CRM login.
    with edge.store._conn() as conn:
        conn.execute('DELETE FROM attendance_sessions');conn.execute('DELETE FROM person_events');conn.execute('DELETE FROM edge_event_queue')
    edge.store.create_arrival(edge.person,'shop',edge.now,'cam',.95,confirmed=False)
    req={'tenant_id':'tenant','edge_id':'edge','absence_started_at':edge.now.isoformat(),'business_date':edge.now.date().isoformat()}
    for i,kind in enumerate(['ATTENDANCE_ENTRY','BREAK_START','BREAK_END','ATTENDANCE_EXIT']):
        req['event']={'event_id':'portal-manual:'+str(i),'person_id':edge.person,'store_id':'shop','camera_id':'cam','event_type':kind,'event_time':edge.now.isoformat(),
            'metadata':{'attendance_session_id':'portal-manual:session','attendance_sync_bridge':True,'attendance_source':'MANUAL','reason_code':'OPERATOR_CHECKOUT','predecessor_event_id':'portal-manual:'+str(i-1) if i else None}}
        assert edge.store.apply_policy_checkout(req)['ok']
        assert edge.store.apply_policy_checkout(req)['duplicate']
    assert edge.store.open_session(edge.person,'shop') is None
    assert len(edge.store.queued_events())==4

def seed_cloud(pg,edge):
    now=edge.now;started=now-timedelta(minutes=65)
    # Match a persisted recognition clock for the local application of the command.
    with edge.store._conn() as c:c.execute('DELETE FROM attendance_observations')
    pg.upsert_crm_person_mapping(dict(tenant_id='tenant',shop_id='shop',local_person_id=edge.person,crm_user_id='user'))
    pg.touch_attendance_presence(tenant_id='tenant',shop_id='shop',local_person_id=edge.person,crm_user_id='user',seen_at=started,camera_id='cam',recognition_event_id='recognition',checked_in=True)
    session=edge.store.open_session(edge.person,'shop')
    payload={'person_id':edge.person,'metadata':{'attendance_session_id':session['id'],'attendance_source':'RECOGNITION'}}
    env=dict(tenant_id='tenant',shop_id='shop',edge_id='edge',camera_id='cam',event_id=session['id'],event_type='ATTENDANCE_ENTRY',event_time=started.isoformat(),payload=payload)
    pg.register_attendance_delivery(env)
    with pg._conn() as c:c.execute(text("UPDATE edge_attendance_delivery SET status='SUCCEEDED',crm_user_id='user'"))
    pg.record_heartbeat(dict(tenant_id='tenant',shop_id='shop',site_id='shop',edge_id='edge',status={'capabilities':['attendance_policy_checkout_v1']}))
    return pg.list_v2_attendance_presence()[0],started

def test_cloud_to_edge_checkout_restart_recovery_and_receipt_owner(pg_release,edge,monkeypatch):
    row,started=seed_cloud(pg_release,edge);calls=[]
    monkeypatch.setattr(api,'store',pg_release);monkeypatch.setenv('CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED','true')
    monkeypatch.setattr(api,'_attendance_coverage_status',lambda *_a,**_k:{'state':'HEALTHY'})
    monkeypatch.setattr(api,'_crm_face_token',lambda *_a:'mock')
    monkeypatch.setattr(api,'crm_client',SimpleNamespace(auto_logout_with_face_token=lambda *_a:calls.append(True) or {'success':True,'message':'test confirmed'}))
    finalize=pg_release.finalize_crm_auto_logout_local
    monkeypatch.setattr(pg_release,'finalize_crm_auto_logout_local',lambda *_a:(_ for _ in ()).throw(RuntimeError('isolated crash')))
    api._v2_auto_logout(row,edge.now)
    monkeypatch.setattr(pg_release,'finalize_crm_auto_logout_local',finalize)
    api._recover_v2_auto_logout_local_finalizations(edge.now)
    api._recover_v2_auto_logout_local_finalizations(edge.now)
    assert calls==[True]
    commands=pg_release.claim_edge_commands('tenant','shop','edge')
    assert len(commands)==1
    assert edge.store.apply_policy_checkout(commands[0]['request'])['closed']
    event=commands[0]['request']['event']
    env=dict(tenant_id='tenant',shop_id='shop',edge_id='edge',camera_id=event['camera_id'],event_id=event['event_id'],event_time=event['event_time'],event_type='ATTENDANCE_EXIT',payload=event)
    assert api._synchronize_attendance_bridge(env)['status']=='SUCCEEDED'
    assert calls==[True]  # Edge acknowledgement cannot send a second CRM logout.
    assert pg_release.list_v2_attendance_presence()==[]

def test_old_exe_and_disabled_flags_do_not_mutate_crm(pg_release,edge,monkeypatch):
    row,_=seed_cloud(pg_release,edge)
    monkeypatch.setattr(api,'store',pg_release)
    monkeypatch.setattr(api,'crm_client',SimpleNamespace(auto_logout_with_face_token=lambda *_a:pytest.fail('Unexpected CRM mutation')))
    monkeypatch.setenv('CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED','false');api._v2_auto_logout(row,edge.now)
    with pg_release._conn() as c:c.execute(text("UPDATE edge_heartbeats SET status_json='{}'::jsonb"))
    monkeypatch.setenv('CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED','true');api._v2_auto_logout(row,edge.now)
    assert pg_release.list_v2_crm_auto_logout_actions('tenant','shop','user')==[]

def test_full_day_absence_is_sticky_and_correction_is_audited(pg_release,edge):
    row,started=seed_cloud(pg_release,edge)
    day=edge.now.date().isoformat()
    args=dict(tenant_id='tenant',shop_id='shop',crm_user_id='user',business_date=day,absence_started_at=started,transition='FULL_DAY_ABSENT',occurred_at=edge.now,details={'localPersonId':edge.person})
    assert pg_release.record_v2_absence_transition(**args)
    args['absence_started_at']+=timedelta(minutes=1)
    assert not pg_release.record_v2_absence_transition(**args)
    pg_release.touch_attendance_presence(tenant_id='tenant',shop_id='shop',local_person_id=edge.person,crm_user_id='user',seen_at=edge.now,camera_id='cam',recognition_event_id='returned',checked_in=None)
    assert pg_release.attendance_day_decisions('tenant','shop')[0]['transition']=='FULL_DAY_ABSENT'
    assert pg_release.attendance_day_decisions('other','shop')==[]
    assert not pg_release.correct_attendance_day('other','shop','user',day,'admin','verified','PRESENT')
    assert pg_release.correct_attendance_day('tenant','shop','user',day,'admin','verified','PRESENT')
    d=pg_release.attendance_day_decisions('tenant','shop')[0]
    assert d['details_json']['actor']=='admin' and d['details_json']['status']=='PRESENT'

def test_coverage_outage_resets_qualifying_clock(pg_release):
    now=datetime.now(UTC);healthy={'state':'HEALTHY'}
    assert pg_release.save_v2_camera_coverage('t','s','u',healthy,now)==now
    assert pg_release.save_v2_camera_coverage('t','s','u',healthy,now+timedelta(minutes=1))==now
    pg_release.save_v2_camera_coverage('t','s','u',{'state':'UNKNOWN'},now+timedelta(minutes=2))
    assert pg_release.save_v2_camera_coverage('t','s','u',healthy,now+timedelta(minutes=65))==now+timedelta(minutes=65)

def test_day_classification_preserves_work_and_local_mirror(edge):
    day=edge.now.astimezone(__import__('zoneinfo').ZoneInfo('Asia/Kolkata')).date().isoformat()
    d=dict(local_person_id=edge.person,business_date=day,transition='FULL_DAY_ABSENT',occurred_at=edge.now.isoformat(),details_json={})
    edge.store.save_attendance_day_decisions('shop',[d])
    result={'_all_items':[dict(person_id=edge.person,login=day+'T09:00:00+05:30',status_label='Checked out',worked_minutes=180,compliance=[])],'summary':{'absent_employees':None}}
    apply_day_decisions(result,edge.store.attendance_day_decisions('shop'),WorkspaceQuery())
    assert result['_all_items'][0]['attendance_day_status']=='ABSENT'
    assert result['_all_items'][0]['worked_minutes']==180
    assert result['summary']['absent_employees']==1

def test_sixty_minutes_marks_day_once_without_crm_activation(pg_release,edge,monkeypatch):
    row,started=seed_cloud(pg_release,edge)
    pg_release.save_v2_camera_coverage('tenant','shop','user',{'state':'HEALTHY'},started)
    # Keep continuity fresh while advancing the isolated clock; no real waiting.
    with pg_release._conn() as c:c.execute(text('UPDATE attendance_camera_coverage SET checked_at=:now'),{'now':edge.now})
    monkeypatch.setattr(api,'store',pg_release);monkeypatch.setenv('CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED','false')
    monkeypatch.setattr(api,'_attendance_coverage_status',lambda *_a,**_k:{'state':'HEALTHY'})
    notices=[];monkeypatch.setattr(api,'_notify_cloud_event',lambda e:notices.append(e))
    api._evaluate_v2_person_absences();api._evaluate_v2_person_absences()
    decisions=pg_release.attendance_day_decisions('tenant','shop')
    assert len(decisions)==1 and decisions[0]['transition']=='FULL_DAY_ABSENT'
    assert len({e['event_id'] for e in notices if e['payload']['metadata']['reason_code']=='FULL_DAY_ABSENT'})==1
    assert pg_release.list_v2_crm_auto_logout_actions('tenant','shop','user')==[]

def test_person_policy_day_end_and_nine_hour_default(pg_release,edge):
    from cloud_portal.policy_resolution import resolve_attendance_policy
    row,started=seed_cloud(pg_release,edge)
    assert resolve_attendance_policy({},{}).values['requiredWorkingMinutes']==540
    login=edge.now-timedelta(hours=10)
    pg_release.record_attendance_activity(dict(id='test-login',tenant_id='tenant',shop_id='shop',crm_user_id='user',local_person_id=edge.person,activity_type='CHECK_IN',occurred_at=login,source='CAMERA_EYE',metadata={},evidence={}))
    pg_release.upsert_person_attendance_policy('tenant','shop','user',{'timezone':'UTC','maxLogoffTime':(edge.now-timedelta(minutes=1)).strftime('%H:%M'),'dayEndAutoLogoutEnabled':True})
    assert pg_release.claim_due_max_logoff_checkouts(edge.now)[0]['local_person_id']==edge.person
    pg_release.complete_presence_checkout('tenant','shop',edge.person,False)
    pg_release.upsert_person_attendance_policy('tenant','shop','user',{'dayEndAutoLogoutEnabled':False})
    assert pg_release.claim_due_max_logoff_checkouts(edge.now)==[]

def test_heartbeat_observations_do_not_reopen_session_or_cross_tenant(pg_release,edge):
    row,started=seed_cloud(pg_release,edge)
    pg_release.record_edge_attendance_observations('other','shop',[{'person_id':edge.person,'seen_at':edge.now.isoformat()}])
    assert pg_release.list_v2_attendance_presence()[0]['last_seen_at']==started
    pg_release.record_edge_attendance_observations('tenant','shop',[{'person_id':edge.person,'seen_at':edge.now.isoformat()}])
    assert pg_release.list_v2_attendance_presence()[0]['last_seen_at']==edge.now
    pg_release.complete_presence_checkout('tenant','shop',edge.person,True)
    pg_release.record_edge_attendance_observations('tenant','shop',[{'person_id':edge.person,'seen_at':edge.now.isoformat()}])
    assert pg_release.list_v2_attendance_presence()==[]

def test_failed_edge_finalization_records_reconciliation(pg_release,edge,monkeypatch):
    row,started=seed_cloud(pg_release,edge)
    bridge=pg_release.active_bridge_session('tenant','shop',edge.person)
    assert pg_release.claim_crm_auto_logout('tenant','shop','user',started,edge.person,'cam','recognition')
    assert pg_release.mark_crm_auto_logout_confirmed('tenant','shop','user',started)
    assert pg_release.finalize_crm_auto_logout_local('tenant','shop','user',started,dict(id='checkout',occurred_at=edge.now,metadata={'bridge_session':bridge},evidence={}))=='SUCCEEDED'
    command=pg_release.claim_edge_commands('tenant','shop','edge')[0]
    assert pg_release.complete_edge_command(command['id'],'tenant','shop','edge','FAILED',{'ok':False,'error':'RECOGNITION_ADVANCED'})
    assert pg_release.list_v2_crm_auto_logout_actions('tenant','shop','user')[0]['status']=='RECONCILIATION_REQUIRED'

def test_cloud_manual_checkout_closes_original_exe_session_and_ack_is_duplicate(pg_release,edge,monkeypatch):
    row,started=seed_cloud(pg_release,edge);session=edge.store.open_session(edge.person,'shop');calls=[]
    env=dict(event_id='portal-manual:checkout',tenant_id='tenant',shop_id='shop',edge_id='edge',camera_id='cam',event_type='ATTENDANCE_EXIT',event_time=edge.now.isoformat(),
        payload={'person_id':edge.person,'store_id':'shop','event_type':'ATTENDANCE_EXIT','metadata':{
            'attendance_session_id':session['id'],'predecessor_event_id':session['id'],'attendance_sync_bridge':True,'attendance_source':'MANUAL'}})
    monkeypatch.setattr(api,'store',pg_release);monkeypatch.setattr(api,'_crm_face_token',lambda *_a:'mock')
    monkeypatch.setattr(api,'crm_client',SimpleNamespace(face_attendance_configured=True,login_logout_with_face_token=lambda *_a:calls.append(True) or {'success':True}))
    assert api._synchronize_attendance_bridge(env)['status']=='SUCCEEDED'
    command=pg_release.claim_edge_commands('tenant','shop','edge')[0]
    assert edge.store.apply_policy_checkout(command['request'])['closed']
    env['payload']=command['request']['event']
    assert api._synchronize_attendance_bridge(env)['status']=='SUCCEEDED'
    assert calls==[True]
