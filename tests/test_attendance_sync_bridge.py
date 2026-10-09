import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException

from camera_service.attendance_engine import AttendanceEngine
from camera_service.attendance_station import AttendanceStation
from camera_service.cloud_client import CloudSyncClient
from camera_service.models import IdentitySeen, PersonnelCreate, PersonnelRole
from camera_service.storage import SQLiteStore
from camera_service.sync_worker import EdgeSyncWorker
from cloud_portal import api
from cloud_portal.storage import PortalStore
from cloud_portal.crm_client import SnapKeyCrmClient


@pytest.fixture
def bridge(tmp_path, monkeypatch):
    monkeypatch.setenv('CAMERA_EYE_ATTENDANCE_MODE','MANUAL')
    monkeypatch.setenv('SNAPKEY_CRM_AUTO_LOGIN_ENABLED','0')
    monkeypatch.setenv('SNAPKEY_CRM_AUTO_LOGOUT_ENABLED','0')
    local=SQLiteStore(str(tmp_path/'edge.db'))
    local.configure_event_scope('tenant','company','shop','edge')
    person=local.create_person(PersonnelCreate(employee_code='E1',full_name='Test',role=PersonnelRole.WORKER))
    engine=AttendanceEngine(local,'shop')
    now=datetime.now(timezone.utc)
    engine.on_identity(IdentitySeen(store_id='shop',camera_id='camera',track_id='track',
        person_id=person['id'],timestamp=now,confidence=.95,bbox=(0,0,10,10)))
    station=AttendanceStation(engine)
    portal=PortalStore(str(tmp_path/'cloud.db'))
    portal.upsert_crm_person_mapping(dict(tenant_id='tenant',shop_id='shop',
        local_person_id=person['id'],crm_user_id='crm-user',break_master_id='lunch'))
    calls=[]
    crm=SimpleNamespace(face_attendance_configured=True,
        login_logout_with_face_token=lambda body,token: calls.append(('attendance',body,token)) or {'success':True},
        business_success=SnapKeyCrmClient.business_success,
        start_break=lambda user,break_id,auth_token: calls.append(('start',user,auth_token)) or {'success':True},
        end_break=lambda user,auth_token: calls.append(('end',user,auth_token)) or {'success':True})
    monkeypatch.setattr(api,'store',portal)
    monkeypatch.setattr(api,'crm_client',crm)
    monkeypatch.setattr(api,'_crm_face_token',lambda tenant,shop,user: 'mock-user-token')
    edge=SimpleNamespace(tenant_id='tenant',company_code='company',shop_id='shop',site_id='site',edge_id='edge')
    client=CloudSyncClient(SimpleNamespace(enabled=True,base_url='https://invalid.test',api_token='mock'))
    principal=api.EdgePrincipal(**vars(edge))
    def apply(action,key):
        candidate=station.candidate('camera',now)
        return station.apply('camera',person['id'],candidate['token'],action,candidate['state'],key,now)
    def envelope(index=0):
        rows=local.queued_events()
        return client.event_envelope(edge,json.loads(rows[index]['payload_json']))
    def ingest(event):
        return api.ingest_edge_event(event,BackgroundTasks(),principal)
    return SimpleNamespace(local=local,portal=portal,station=station,person=person,now=now,
        apply=apply,envelope=envelope,ingest=ingest,calls=calls,crm=crm,edge=edge)


def test_four_actions_sync_once_with_stable_session_and_reentry(bridge):
    for action,key in [('CHECK_IN','in'),('START_BREAK','break'),('END_BREAK','end'),('CHECK_OUT','out')]:
        bridge.apply(action,key)
    events=[bridge.envelope(i) for i in range(4)]
    session_ids={event['payload']['metadata']['attendance_session_id'] for event in events}
    assert len(session_ids)==1
    for event in events:
        assert bridge.ingest(event)['attendance_sync']['status']=='SUCCEEDED'
        assert bridge.ingest(event)['attendance_sync']['status']=='SUCCEEDED'
    assert len(bridge.calls)==4
    assert all(call[-1]=='mock-user-token' for call in bridge.calls)
    assert bridge.calls[0][1]['userId']=='crm-user'
    assert bridge.calls[0][1]['actualStartTime'] and bridge.calls[3][1]['actualOffTime']
    new=bridge.apply('CHECK_IN','reentry')
    assert new['session_id'] not in session_ids
    assert bridge.ingest(bridge.envelope(4))['attendance_sync']['status']=='SUCCEEDED'


def test_atomic_queue_failure_rolls_back_attendance(bridge,monkeypatch):
    def fail(*args): raise RuntimeError('simulated queue write failure')
    monkeypatch.setattr(bridge.local,'_enqueue_edge_event',fail)
    with pytest.raises(RuntimeError): bridge.apply('CHECK_IN','in')
    assert not bridge.local.open_session(bridge.person['id'],'shop')['entry_confirmed']
    assert bridge.local.person_events()==[]


@pytest.mark.parametrize('age,fresh',[(0,True),(3600,False)])
def test_manual_evidence_preserves_fresh_media_without_reusing_old_clip(bridge,age,fresh):
    parent=bridge.local.add_person_event(bridge.person['id'],'shop','camera','PERSON_RECOGNIZED',
        bridge.now-timedelta(seconds=age),{'snapshot_paths':['one.jpg','two.jpg','three.jpg'],'clip_path':'clip.mp4'})
    bridge.apply('CHECK_IN','in')
    row=next(row for row in bridge.local.queued_events() if row['event_type']=='ATTENDANCE_ENTRY')
    metadata=json.loads(row['payload_json'])['metadata']
    assert bool(metadata['clip_path']) is fresh
    if fresh:
        assert metadata['recognition_event_id']==parent
        assert metadata['snapshot_paths']==['one.jpg','two.jpg','three.jpg']
    else:
        assert metadata['evidence_missing']['clip']=='recognition_clip_not_available'


def test_pre_bridge_open_session_can_start_manual_chain(bridge):
    with bridge.local._conn() as conn:
        conn.execute("UPDATE attendance_sessions SET entry_confirmed=1 WHERE person_id=?",
                     (bridge.person['id'],))
    result=bridge.apply('START_BREAK','legacy-break')
    assert result['applied']
    row=next(row for row in bridge.local.queued_events() if row['event_type']=='BREAK_START')
    metadata=json.loads(row['payload_json'])['metadata']
    assert metadata['predecessor_event_id'] is None
    assert metadata['legacy_session_root'] is True
    assert bridge.ingest(bridge.envelope())['attendance_sync']['status']=='SUCCEEDED'


def test_recognition_event_is_never_attendance_predecessor(bridge):
    bridge.local.add_person_event(
        bridge.person['id'],'shop','camera','PERSON_RECOGNIZED',bridge.now,
        {'attendance_sync_bridge':True,'snapshot_path':'recognition.jpg'})
    bridge.apply('CHECK_IN','in')
    row=next(row for row in bridge.local.queued_events() if row['event_type']=='ATTENDANCE_ENTRY')
    metadata=json.loads(row['payload_json'])['metadata']
    assert metadata['predecessor_event_id'] is None
    assert bridge.ingest(bridge.envelope(1))['attendance_sync']['status']=='SUCCEEDED'


def test_cloud_repairs_deployed_auto_entry_with_recognition_predecessor(bridge,monkeypatch):
    """A pre-fix edge may have persisted PERSON_RECOGNIZED as the predecessor."""
    monkeypatch.setenv('SNAPKEY_CRM_AUTO_LOGIN_ENABLED','1')
    recognized={
        'schema_version':'edge.event.v1','tenant_id':'tenant','company_code':'company',
        'shop_id':'shop','site_id':'site','edge_id':'edge','camera_id':'camera',
        'event_id':'recognition-old','event_type':'PERSON_RECOGNIZED',
        'event_time':bridge.now.isoformat(),'payload':{
            'event_id':'recognition-old','person_id':bridge.person['id'],
            'event_type':'PERSON_RECOGNIZED','event_time':bridge.now.isoformat(),
            'metadata':{'attendance_sync_bridge':True}},
    }
    bridge.portal.ingest_event(recognized)
    entry=copy.deepcopy(recognized)
    entry.update(event_id='entry-old',event_type='ATTENDANCE_ENTRY')
    entry['payload']={
        'event_id':'entry-old','person_id':bridge.person['id'],'event_type':'ATTENDANCE_ENTRY',
        'event_time':bridge.now.isoformat(),'metadata':{
            'attendance_sync_bridge':True,'attendance_source':'RECOGNITION',
            'attendance_mode':'AUTO','attendance_session_id':'session-old',
            'predecessor_event_id':'recognition-old'},
    }
    result=bridge.ingest(entry)
    assert result['attendance_sync']['status']=='SUCCEEDED'
    receipt=bridge.portal.attendance_delivery_receipt('tenant','shop','entry-old')
    assert receipt['predecessor_id']=='recognition-old'
    assert receipt['effective_predecessor_id'] is None
    assert receipt['registration_reason']=='IGNORED_NON_ATTENDANCE_PREDECESSOR'
    assert len(bridge.calls)==1


def test_existing_explicit_break_routes_use_the_same_session_queue(bridge):
    checked_in=bridge.apply('CHECK_IN','in')
    bridge.station.engine.start_break(bridge.person['id'],'camera',bridge.now)
    with pytest.raises(ValueError): bridge.station.engine.start_break(bridge.person['id'],'camera',bridge.now)
    bridge.station.engine.end_break(bridge.person['id'],'camera',bridge.now)
    for i in range(3):
        envelope=bridge.envelope(i)
        assert envelope['payload']['metadata']['attendance_session_id']==checked_in['session_id']
        assert bridge.ingest(envelope)['attendance_sync']['status']=='SUCCEEDED'
    assert len(bridge.calls)==3


def test_duplicate_request_survives_candidate_expiry_and_restart(bridge):
    candidate=bridge.station.candidate('camera',bridge.now)
    result=bridge.apply('CHECK_IN','in')
    bridge.station.engine.store=SQLiteStore(bridge.local.path)
    duplicate=bridge.station.apply('camera',bridge.person['id'],candidate['token'],'CHECK_IN','OUT','in',
        bridge.now+timedelta(hours=1))
    assert duplicate['duplicate'] and duplicate['session_id']==result['session_id']
    assert len(bridge.local.queued_events())==1


def test_out_of_order_delivery_waits_for_predecessor(bridge):
    bridge.apply('CHECK_IN','in'); bridge.apply('START_BREAK','break')
    assert bridge.ingest(bridge.envelope(1))['attendance_sync']['status']=='WAITING_PREDECESSOR'
    assert bridge.calls==[]
    assert bridge.ingest(bridge.envelope())['attendance_sync']['status']=='SUCCEEDED'
    assert bridge.ingest(bridge.envelope(1))['attendance_sync']['status']=='SUCCEEDED'


def test_ingested_break_registers_before_mapping_and_delivers_after_mapping(bridge):
    bridge.apply('CHECK_IN','in'); bridge.ingest(bridge.envelope())
    bridge.apply('START_BREAK','break')
    event=bridge.envelope(1)
    with bridge.portal._conn() as conn:
        conn.execute("DELETE FROM crm_person_mappings WHERE tenant_id=? AND shop_id=?",
                     ('tenant','shop'))
    result=bridge.ingest(event)
    assert result['attendance_sync']=={'status':'MAPPING_REQUIRED','attempts':0}
    receipt=bridge.portal.attendance_delivery_receipt('tenant','shop',event['event_id'])
    assert receipt['status']=='MAPPING_REQUIRED' and receipt['attempts']==0
    assert len(bridge.calls)==1  # check-in only
    bridge.portal.upsert_crm_person_mapping(dict(tenant_id='tenant',shop_id='shop',
        local_person_id=bridge.person['id'],crm_user_id='crm-user',break_master_id='lunch'))
    assert bridge.ingest(event)['attendance_sync']['status']=='SUCCEEDED'
    assert [call[0] for call in bridge.calls]==['attendance','start']


def test_missing_historical_break_predecessor_is_quarantined_until_operator_review(bridge):
    bridge.apply('CHECK_IN','in'); bridge.apply('START_BREAK','start'); bridge.apply('END_BREAK','end')
    entry,start,end=(bridge.envelope(i) for i in range(3))
    bridge.ingest(entry)
    # Reproduce production: immutable BREAK_START exists, but its delivery receipt does not.
    bridge.portal.ingest_event(start)
    result=bridge.ingest(end)
    assert result['attendance_sync']['status']=='WAITING_PREDECESSOR'
    start_receipt=bridge.portal.attendance_delivery_receipt('tenant','shop',start['event_id'])
    assert start_receipt['status']=='RECONCILIATION_REQUIRED'
    assert start_receipt['registration_reason']=='HISTORICAL_RECEIPT_MISSING'
    assert start_receipt['attempts']==0
    assert [call[0] for call in bridge.calls]==['attendance']

    admin=api.PortalPrincipal('session','tenant','company','shop','admin','Admin','OWNER')
    review=api.AttendanceSyncReconciliation(
        crm_applied=False,justification='CRM roster verified: break start was not applied')
    assert api.reconcile_attendance_sync('tenant',start['event_id'],review,admin)['status']=='SUCCEEDED'
    assert [call[0] for call in bridge.calls]==['attendance','start']
    assert bridge.ingest(end)['attendance_sync']['status']=='SUCCEEDED'
    assert [call[0] for call in bridge.calls]==['attendance','start','end']


def test_duplicate_break_start_ingestion_recovers_interrupted_registration_safely(bridge):
    """A persisted event with no receipt may have been interrupted before registration."""
    bridge.apply('CHECK_IN','in'); bridge.apply('START_BREAK','start')
    entry,start=(bridge.envelope(i) for i in range(2))
    assert bridge.ingest(entry)['attendance_sync']['status']=='SUCCEEDED'
    # Simulate a process interruption after immutable ingestion, before receipt creation.
    bridge.portal.ingest_event(start)

    recovered=bridge.ingest(start)
    receipt=bridge.portal.attendance_delivery_receipt('tenant','shop',start['event_id'])
    assert recovered['attendance_sync']['status']=='RECONCILIATION_REQUIRED'
    assert receipt['status']=='RECONCILIATION_REQUIRED'
    assert receipt['registration_reason']=='HISTORICAL_RECEIPT_MISSING'
    assert receipt['attempts']==0
    assert [call[0] for call in bridge.calls]==['attendance']


def retry_now(bridge):
    with bridge.portal._conn() as conn:
        conn.execute('UPDATE edge_attendance_delivery SET next_attempt_at=NULL')


def test_rejected_crm_request_retries_after_duplicate_ingestion(bridge):
    bridge.apply('CHECK_IN','in')
    original=bridge.crm.login_logout_with_face_token
    bridge.crm.login_logout_with_face_token=lambda *args: {'success':False}
    assert bridge.ingest(bridge.envelope())['attendance_sync']['status']=='RETRY'
    bridge.crm.login_logout_with_face_token=original
    retry_now(bridge)
    assert bridge.ingest(bridge.envelope())['attendance_sync']['status']=='SUCCEEDED'
    assert len(bridge.calls)==1


def test_timeout_requires_reconciliation_and_does_not_replay(bridge):
    bridge.apply('CHECK_IN','in')
    def timeout(*args):
        bridge.calls.append('attempt')
        raise httpx.ReadTimeout('possibly accepted')
    bridge.crm.login_logout_with_face_token=timeout
    for _ in range(2):
        assert bridge.ingest(bridge.envelope())['attendance_sync']['status']=='RECONCILIATION_REQUIRED'
    assert bridge.calls==['attempt']


def test_reconciliation_requires_scoped_admin_and_does_not_repeat_confirmed_crm(bridge):
    bridge.apply('CHECK_IN','in'); event=bridge.envelope()
    bridge.portal.ingest_event(event)
    bridge.portal.claim_attendance_delivery(event,'crm-user')
    bridge.portal.set_attendance_delivery(event,'RECONCILIATION_REQUIRED')
    admin=api.PortalPrincipal('session','tenant','company','shop','admin','Admin','OWNER')
    request=api.AttendanceSyncReconciliation(crm_applied=True,justification='CRM action verified by administrator')
    viewer=api.PortalPrincipal('session','tenant','company','shop','viewer','Viewer','VIEWER')
    with pytest.raises(HTTPException) as error:
        api.reconcile_attendance_sync('tenant',event['event_id'],request,viewer)
    assert error.value.status_code==403
    other=api.PortalPrincipal('session','tenant','company','other-shop','admin','Admin','OWNER')
    with pytest.raises(HTTPException): api.reconcile_attendance_sync('tenant',event['event_id'],request,other)
    assert api.reconcile_attendance_sync('tenant',event['event_id'],request,admin)['status']=='SUCCEEDED'
    receipt=api.attendance_sync_receipt('tenant',event['event_id'],admin)
    assert receipt['reconciled_by']=='admin'
    assert bridge.ingest(event)['attendance_sync']['status']=='SUCCEEDED'
    assert bridge.calls==[]


def test_confirmed_crm_success_retries_only_local_finalization(bridge,monkeypatch):
    bridge.apply('CHECK_IN','in')
    state={'fail':True,'records':[]}
    def record(item):
        if state['fail']: raise RuntimeError('local persistence unavailable')
        state['records'].append(item)
    monkeypatch.setattr(bridge.portal,'record_attendance_activity',record,raising=False)
    assert bridge.ingest(bridge.envelope())['attendance_sync']['status']=='CRM_CONFIRMED'
    state['fail']=False; retry_now(bridge)
    assert bridge.ingest(bridge.envelope())['attendance_sync']['status']=='SUCCEEDED'
    assert len(bridge.calls)==1 and len(state['records'])==1
    assert state['records'][0]['metadata']['attendance_session_id']


def test_cross_tenant_and_altered_duplicate_are_rejected(bridge):
    bridge.apply('CHECK_IN','in')
    event=bridge.envelope()
    wrong=copy.deepcopy(event); wrong['tenant_id']='other'
    with pytest.raises(HTTPException) as error: bridge.ingest(wrong)
    assert error.value.status_code==403
    assert bridge.calls==[]
    bridge.ingest(event)
    wrong=copy.deepcopy(event); wrong['payload']['person_id']='different-person'
    with pytest.raises(HTTPException) as error: bridge.ingest(wrong)
    assert error.value.status_code==409
    assert len(bridge.calls)==1


def test_missing_mapping_cannot_use_client_supplied_crm_identity(bridge):
    bridge.apply('CHECK_IN','in')
    event=bridge.envelope(); event['payload']['person_id']='unmapped'
    event['payload']['crm_user_id']='crm-user'
    assert bridge.ingest(event)['attendance_sync']['status']=='MAPPING_REQUIRED'
    assert bridge.calls==[]
    receipt=bridge.portal.attendance_delivery_receipt('tenant','shop',event['event_id'])
    assert receipt['status']=='MAPPING_REQUIRED' and receipt['attempts']==0


def test_confirmed_reconciliation_never_falls_back_to_dispatch_when_mapping_is_temporarily_missing(bridge):
    bridge.apply('CHECK_IN','in'); event=bridge.envelope()
    bridge.portal.ingest_event(event)
    bridge.portal.claim_attendance_delivery(event,'crm-user')
    bridge.portal.set_attendance_delivery(event,'RECONCILIATION_REQUIRED')
    with bridge.portal._conn() as conn:
        conn.execute("DELETE FROM crm_person_mappings WHERE tenant_id=? AND shop_id=?",('tenant','shop'))
    admin=api.PortalPrincipal('session','tenant','company','shop','admin','Admin','OWNER')
    request=api.AttendanceSyncReconciliation(
        crm_applied=True,justification='Verified matching check-in in CRM before recovery')
    result=api.reconcile_attendance_sync('tenant',event['event_id'],request,admin)
    assert result['status']=='CRM_CONFIRMED' and result['error_code']=='CRM_MAPPING_REQUIRED'
    assert bridge.calls==[]
    bridge.portal.upsert_crm_person_mapping(dict(tenant_id='tenant',shop_id='shop',
        local_person_id=bridge.person['id'],crm_user_id='crm-user',break_master_id='lunch'))
    assert bridge.ingest(event)['attendance_sync']['status']=='SUCCEEDED'
    assert bridge.calls==[]


def test_scoped_readiness_reports_flags_and_break_mapping_without_secrets(bridge,monkeypatch):
    bridge.apply('CHECK_IN','in'); event=bridge.envelope()
    bridge.portal.ingest_event(event)
    admin=api.PortalPrincipal('session','tenant','company','shop','admin','Admin','OWNER')
    monkeypatch.delenv('CAMERA_EYE_TOKEN_ENCRYPTION_KEY',raising=False)
    result=api.attendance_sync_readiness('tenant',event['event_id'],admin)
    assert result['ready'] is False
    assert result['blockers']==['TOKEN_ENCRYPTION_KEY_REQUIRED']
    assert set(result)=={'event_id','event_type','attendance_source','ready','blockers','receipt'}


def test_multiworker_claim_and_interrupted_claim(bridge):
    bridge.apply('CHECK_IN','in'); event=bridge.envelope()
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(lambda _:bridge.portal.claim_attendance_delivery(event,'crm-user'),range(2)))
    assert sum(claimed for _,claimed in outcomes)==1
    status,claimed=bridge.portal.claim_attendance_delivery(event,'crm-user',bridge.now+timedelta(minutes=6))
    assert status=='RECONCILIATION_REQUIRED' and not claimed


def test_cross_session_break_is_rejected(bridge):
    bridge.apply('CHECK_IN','in'); bridge.apply('START_BREAK','break')
    bridge.ingest(bridge.envelope())
    broken=bridge.envelope(1)
    broken['payload']['metadata']['attendance_session_id']='different-session'
    with pytest.raises(HTTPException) as error: bridge.ingest(broken)
    assert error.value.status_code==409
    assert len(bridge.calls)==1


def test_duplicate_session_action_with_another_event_id_is_rejected(bridge):
    bridge.apply('CHECK_IN','in'); event=bridge.envelope()
    bridge.ingest(event)
    changed=copy.deepcopy(event); changed['event_id']='duplicate-action'
    changed['payload']['event_id']='duplicate-action'
    with pytest.raises(HTTPException) as error: bridge.ingest(changed)
    assert error.value.status_code==409
    assert len(bridge.calls)==1


def test_automatic_entry_respects_manual_policy_and_auto_flag(bridge,monkeypatch):
    bridge.apply('CHECK_IN','in'); event=bridge.envelope()
    event['payload']['metadata']['attendance_source']='RECOGNITION'
    monkeypatch.setattr(bridge.portal,'person_attendance_policy',lambda *_:{'attendanceMode':'MANUAL'},raising=False)
    assert bridge.ingest(event)['attendance_sync']['status']=='RETRY'
    assert bridge.calls==[]
    monkeypatch.setattr(bridge.portal,'person_attendance_policy',lambda *_:{'attendanceMode':'AUTO'})
    retry_now(bridge)
    assert bridge.ingest(event)['attendance_sync']['status']=='RETRY'
    assert bridge.calls==[]
    monkeypatch.setenv('SNAPKEY_CRM_AUTO_LOGIN_ENABLED','1'); retry_now(bridge)
    assert bridge.ingest(event)['attendance_sync']['status']=='SUCCEEDED'


def test_bridge_recognition_does_not_create_duplicate_cloud_checkin(bridge):
    api._auto_attend_recognized_person({'event_type':'PERSON_RECOGNIZED',
        'payload':{'person_id':bridge.person['id'],'metadata':{'attendance_sync_bridge':True}}})
    assert bridge.calls==[]


def test_enabled_absence_logout_still_cannot_close_bridge_session(bridge,monkeypatch):
    bridge.apply('CHECK_IN','in'); bridge.ingest(bridge.envelope())
    monkeypatch.setenv('CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED','true')
    api._v2_auto_logout({'tenant_id':'tenant','shop_id':'shop','crm_user_id':'crm-user',
        'local_person_id':bridge.person['id'],'checked_in':True,'on_break':False,
        'last_seen_at':bridge.now-timedelta(hours=2),'policy_json':{}},bridge.now)
    assert len(bridge.calls)==1


def test_401_retry_invalidates_only_employee_token(bridge,monkeypatch):
    bridge.apply('CHECK_IN','in'); invalidated=[]
    monkeypatch.setattr(api,'_invalidate_crm_face_token_on_401',lambda *args:invalidated.append(args[:3]))
    original=bridge.crm.login_logout_with_face_token
    def unauthorized(*args):
        response=httpx.Response(401,request=httpx.Request('POST','https://crm.invalid'))
        raise httpx.HTTPStatusError('unauthorized',request=response.request,response=response)
    bridge.crm.login_logout_with_face_token=unauthorized
    assert bridge.ingest(bridge.envelope())['attendance_sync']['status']=='RETRY'
    assert invalidated==[('tenant','shop','crm-user')]
    bridge.crm.login_logout_with_face_token=original; retry_now(bridge)
    assert bridge.ingest(bridge.envelope())['attendance_sync']['status']=='SUCCEEDED'


def test_worker_keeps_pending_until_cloud_confirms_crm(bridge):
    bridge.apply('CHECK_IN','in')
    status={'value':'RETRY'}
    cloud=SimpleNamespace(enabled=lambda:True,post_event=lambda *_:{'ok':True,'attendance_sync':{'status':status['value']}})
    license=SimpleNamespace(status=lambda:SimpleNamespace(active=True))
    worker=EdgeSyncWorker(bridge.local,cloud,bridge.edge,SimpleNamespace(batch_size=50),license)
    assert worker.run_once().failed==1
    assert bridge.local.event_queue_status()['counts']=={'PENDING':1}
    with bridge.local._conn() as conn: conn.execute('UPDATE edge_event_queue SET next_attempt_at=NULL')
    status['value']='SUCCEEDED'
    assert worker.run_once().synced==1
    assert bridge.local.event_queue_status()['counts']=={'SYNCED':1}


def test_person_mode_overrides_global_auto_and_track_loss_never_checks_out(bridge,monkeypatch):
    monkeypatch.setenv('CAMERA_EYE_ATTENDANCE_MODE','AUTO')
    with bridge.local._conn() as conn:
        conn.execute("UPDATE personnel SET attendance_mode='MANUAL' WHERE id=?",(bridge.person['id'],))
    ev=bridge.station.engine.identities[('camera','track')]
    bridge.station.engine.on_identity(ev)
    assert bridge.local.queued_events()==[]
    bridge.apply('CHECK_IN','in')
    bridge.station.engine.on_track_lost('camera','track')
    assert bridge.local.open_session(bridge.person['id'],'shop')['entry_confirmed']
    assert [row['event_type'] for row in bridge.local.queued_events()]==['ATTENDANCE_ENTRY']


def test_auto_session_inherits_completed_recognition_snapshots_and_clip(bridge,monkeypatch):
    monkeypatch.setenv('CAMERA_EYE_ATTENDANCE_MODE','AUTO')
    parent=bridge.local.add_person_event(bridge.person['id'],'shop','camera','PERSON_RECOGNIZED',bridge.now,
        {'snapshot_path':'first.jpg','snapshot_paths':['first.jpg'],'evidence_pending':True})
    bridge.station.engine.on_identity(bridge.station.engine.identities[('camera','track')],recognition_event_id=parent)
    session=bridge.local.open_session(bridge.person['id'],'shop')
    bridge.local.update_person_event_evidence(parent,snapshot_paths=['first.jpg','second.jpg','third.jpg'],
        clip_path='recognition.mp4',evidence_pending=False)
    queued=next(row for row in bridge.local.queued_events() if row['id']==session['id'])
    metadata=json.loads(queued['payload_json'])['metadata']
    assert metadata['attendance_sync_bridge'] and metadata['attendance_session_id']==session['id']
    assert metadata['snapshot_paths']==['first.jpg','second.jpg','third.jpg']
    assert metadata['clip_path']=='recognition.mp4' and not metadata['evidence_pending']
