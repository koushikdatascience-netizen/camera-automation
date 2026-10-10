"""Synthetic attendance; no live camera, CRM requests or production databases."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime,timedelta,timezone
import csv
import io
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from camera_service.storage import SQLiteStore
from camera_service.models import IdentitySeen,PersonnelCreate,PersonnelRole
from camera_service.attendance_engine import AttendanceEngine
from camera_service.attendance_station import AttendanceStation
from camera_service.attendance_workspace import WorkspaceQuery,build_workspace,policy_snapshot,export_csv,normalize_event
from camera_service.attendance_workspace_api import local_sources

UTC=timezone.utc
NOW=datetime(2026,10,10,12,tzinfo=UTC)


def data():
    person={'id':'alice','full_name':'Alice Synthetic','employee_code':'SYN-A','active':True}
    policy={'timezone':'UTC','total_working_minutes':60,'allowed_break_minutes':10,'max_logoff_time':'23:59'}
    snap=policy_snapshot(policy)
    events=[]
    for eid,kind,at in [('in','ATTENDANCE_ENTRY','2026-10-09T09:00:00Z'),('b1','BREAK_START','2026-10-09T09:30:00Z'),('b2','BREAK_END','2026-10-09T09:45:00Z'),('out','ATTENDANCE_EXIT','2026-10-09T11:00:00Z')]:
        events.append({'id':eid,'event_type':kind,'event_time':at,'camera_id':'entrance','shop_id':'shop','payload':{'person_id':'alice',
            'metadata':{'attendance_session_id':'session','attendance_source':'MANUAL','attendance_policy_snapshot':snap}}})
    return [person],events,{'alice':policy}


def report(**filters):
    people,events,policies=data()
    query=WorkspaceQuery(preset='custom',start='2026-10-09',end='2026-10-09',timezone='UTC',**filters)
    return build_workspace(people,[],events,query,now=NOW,policies=policies)


def test_explicit_manual_timeline_durations_snapshot_and_checkout():
    result=report();row=result['items'][0]
    assert [e['activity'] for e in result['events']]==['LOGIN','BREAK_IN','BREAK_OUT','LOGOUT']
    assert [e['new_state'] for e in result['events']]==['IN','ON_BREAK','IN','OUT']
    assert row['worked_minutes']==105 and row['break_minutes']==15 and row['status']=='OUT'
    assert result['summary']['overtime_minutes']==45
    assert row['policy_is_historical'] and 'BREAK_LIMIT_EXCEEDED' in row['compliance']
    assert row['evidence_status']=='Evidence unavailable'


def test_local_workspace_keeps_crm_message_after_sync_acknowledgement(station):
    st,now,person,_event=station
    action=apply(st,now,'CHECK_IN','crm-message')
    queued=next(row for row in st.engine.store.queued_events(20)
                if row['id']==action['event_id'])
    st.engine.store.mark_event_synced(queued['id'],'Logged in successfully.')
    people,sessions,events=local_sources(st.engine.store,'shop')
    row=next(item for item in events if item['id']==queued['id'])
    normalized=normalize_event(row)
    assert normalized['crm_message']=='Logged in successfully.'


def test_timeline_exposes_safe_crm_success_and_rejection_messages():
    success=normalize_event({'id':'entry','event_type':'ATTENDANCE_ENTRY',
        'event_time':'2026-10-09T09:00:00Z','person_id':'alice',
        'crm_message':'Logged in successfully.',
        'metadata':{'attendance_session_id':'session','attendance_source':'MANUAL'}})
    rejected=normalize_event({'id':'rejected','event_type':'ATTENDANCE_ENTRY',
        'event_time':'2026-10-09T09:00:00Z','person_id':'alice',
        'crm_error':'actualStartTime: must be a valid TimeSpan.',
        'metadata':{'attendance_session_id':'session','attendance_source':'MANUAL'}})
    assert success['crm_message']=='Logged in successfully.'
    assert rejected['crm_error']=='actualStartTime: must be a valid TimeSpan.'


@pytest.mark.parametrize('filters',[{'search':'unmatched'},{'person_id':'other'},{'camera_id':'wrong'},{'shop_id':'wrong'},{'status':'IN'},{'source':'AUTO'},{'activity':'AUTO_LOGIN'}])
def test_filters_apply_to_sessions_summary_timeline_and_export(filters):
    result=report(**filters)
    assert result['items']==[] and result['events']==[]
    assert result['summary']['worked_minutes']==0
    assert list(csv.DictReader(io.StringIO(export_csv(result['_all_items']).decode('utf-8-sig'))))==[]


def test_pagination_does_not_change_summary_or_export():
    result=report(page=2,page_size=1)
    assert result['items']==[] and result['total']==1 and result['summary']['worked_minutes']==105
    assert len(list(csv.DictReader(io.StringIO(export_csv(result['_all_items']).decode('utf-8-sig')))))==1


def test_overnight_breaks_clip_to_each_business_day_and_timezone():
    people,events,policies=data()
    for e,at in zip(events,['2026-10-09T23:50:00Z','2026-10-09T23:55:00Z','2026-10-10T00:10:00Z','2026-10-10T00:20:00Z']):e['event_time']=at
    first=build_workspace(people,[],events,WorkspaceQuery(preset='custom',start='2026-10-09',end='2026-10-09',timezone='UTC'),now=NOW,policies=policies)
    second=build_workspace(people,[],events,WorkspaceQuery(preset='today',timezone='UTC'),now=NOW,policies=policies)
    assert first['items'][0]['worked_minutes']==5 and first['items'][0]['break_minutes']==5
    assert second['items'][0]['worked_minutes']==10 and second['items'][0]['break_minutes']==10
    indian=build_workspace(people,[],events,WorkspaceQuery(preset='today',timezone='Asia/Kolkata'),now=NOW,policies=policies)
    assert indian['items'][0]['login'].endswith('+05:30') and indian['summary']['worked_minutes']==15


def test_legacy_session_source_is_not_invented_and_unzoned_events_warn():
    people,_,policies=data()
    sessions=[dict(id='legacy',person_id='alice',store_id='shop',entry_confirmed=1,arrival_time='2026-10-09T09:00:00Z',exit_time='2026-10-09T10:00:00Z')]
    result=build_workspace(people,sessions,[],WorkspaceQuery(preset='yesterday',timezone='UTC'),now=NOW,policies=policies)
    assert result['events']==[] and result['items'][0]['source']=='UNKNOWN'
    assert result['items'][0]['policy_is_historical'] is False
    result=build_workspace(people,sessions,[dict(id='invalid',person_id='alice',event_type='LOGIN',event_time='2026-10-09T09:00:00')],WorkspaceQuery(preset='yesterday',timezone='UTC'),now=NOW)
    assert result['warnings'] and not result['events']


def test_export_sanitizes_spreadsheet_formula_injection():
    row=report()['items'][0];row['employee_name']=' =HYPERLINK("malicious")'
    exported=list(csv.DictReader(io.StringIO(export_csv([row]).decode('utf-8-sig'))))[0]
    assert exported['employee_name'].startswith("'")


def test_zero_worked_duration_sort_has_consistent_numeric_keys():
    people,events,policies=data()
    events.append(dict(id='zero-login',person_id='alice',event_type='LOGIN',event_time='2026-10-09T12:00:00Z',metadata={'attendance_session_id':'zero'}))
    events.append(dict(id='zero-logout',person_id='alice',event_type='LOGOUT',event_time='2026-10-09T12:00:00Z',metadata={'attendance_session_id':'zero'}))
    result=build_workspace(people,[],events,WorkspaceQuery(preset='yesterday',timezone='UTC',sort='worked',direction='asc'),now=NOW,policies=policies)
    assert result['items'][0]['worked_minutes']==0


def test_grace_warning_does_not_invent_pending_logout_when_no_action_queued():
    people,events,policies=data();events=events[:1]
    grace={'id':'grace','person_id':'alice','event_type':'GRACE_EXCEEDED','event_time':'2026-10-09T10:00:00Z','metadata':{'attendance_session_id':'session'}}
    events.append(grace)
    query=WorkspaceQuery(preset='yesterday',timezone='UTC')
    result=build_workspace(people,[],events,query,now=NOW,policies=policies)
    assert result['items'][0]['status']=='IN'
    assert result['events'][-1]['activity']=='GRACE_PERIOD_EXPIRED'
    grace['metadata']['new_state']='PENDING_AUTO_LOGOUT'
    result=build_workspace(people,[],events,query,now=NOW,policies=policies)
    assert result['items'][0]['status']=='PENDING_AUTO_LOGOUT'


def test_complete_session_timeline_includes_prior_day_login():
    people,events,policies=data()
    events[0]['event_time']='2026-10-08T23:59:00Z'
    query=WorkspaceQuery(preset='yesterday',timezone='UTC',session_id='session',full_session=True)
    result=build_workspace(people,[],events,query,now=NOW,policies=policies)
    assert len(result['events'])==4 and result['events'][0]['timestamp'].startswith('2026-10-08')
    with pytest.raises(ValueError):WorkspaceQuery(full_session=True)


@pytest.fixture
def station(tmp_path,monkeypatch):
    monkeypatch.setenv('CAMERA_EYE_ATTENDANCE_MODE','MANUAL')
    store=SQLiteStore(str(tmp_path/'test.db'))
    p=store.create_person(PersonnelCreate(employee_code='TEST',full_name='Synthetic Operator',role=PersonnelRole.WORKER))
    engine=AttendanceEngine(store,'shop');st=AttendanceStation(engine)
    now=datetime.now(UTC)
    event=IdentitySeen(store_id='shop',camera_id='entrance',track_id='1',person_id=p['id'],timestamp=now,confidence=.95,bbox=(0,0,100,100))
    engine.on_identity(event)
    return st,now,p,event


def apply(st,at,action,key):
    candidate=st.candidate('entrance',at)
    return st.apply('entrance',candidate['person_id'],candidate['token'],action,candidate['state'],key,at)


def test_checkout_ends_break_restart_keeps_state_and_events(station):
    st,now,p,event=station
    apply(st,now,'CHECK_IN','in');apply(st,now,'START_BREAK','break')
    restarted=AttendanceEngine(st.engine.store,'shop')
    restarted.on_identity(event)
    assert restarted.presence[p['id']].status=='BREAK'
    assert st.engine.store.open_session(p['id'],'shop')['break_started_at']
    apply(st,now+timedelta(seconds=1),'CHECK_OUT','out')
    row=st.engine.store.attendance()[0]
    assert row['break_started_at'] is None and row['last_break_end']==row['exit_time']
    record=next(e for e in st.engine.store.person_events() if e['id']=='manual:out')
    assert json.loads(record['metadata_json'])['break_closed_by_checkout'] is True
    assert len(st.engine.store.person_events())==3  # No fabricated BREAK_OUT.


def test_shop_attendance_mode_overrides_legacy_person_column(tmp_path):
    store=SQLiteStore(str(tmp_path/'shop-mode.db'))
    person=store.create_person(PersonnelCreate(employee_code='SHOP-MODE',full_name='Shop Mode',role=PersonnelRole.WORKER))
    with store._conn() as conn:
        conn.execute("UPDATE personnel SET attendance_mode='AUTO' WHERE id=?",(person['id'],))
        conn.execute("INSERT INTO attendance_workspace_policies VALUES(?,?,?,?)",
                     ('shop','',json.dumps({'attendance_mode':'MANUAL'}),store.now()))
    event=IdentitySeen(store_id='shop',camera_id='entrance',track_id='track',person_id=person['id'],
        timestamp=NOW,confidence=.99,bbox=(0,0,100,100))
    AttendanceEngine(store,'shop').on_identity(event)
    assert not store.open_session(person['id'],'shop')['entry_confirmed']


def test_concurrent_manual_and_auto_login_have_one_confirmed_session(station):
    st,now,p,event=station
    def auto():return st.engine.store.create_arrival(p['id'],'shop',now,'entrance',.95,confirmed=True)
    def manual():
        try:return apply(st,now,'CHECK_IN','race')
        except ValueError:return None
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(lambda fn:fn(),[auto,manual]))
    rows=st.engine.store.attendance()
    assert len(rows)==1 and rows[0]['entry_confirmed']
    assert len([e for e in st.engine.store.queued_events() if e['event_type']=='ATTENDANCE_ENTRY'])==1


def test_local_api_filters_policy_and_media_scope(station,tmp_path,monkeypatch):
    import camera_service.api as api
    st,now,p,event=station;apply(st,now,'CHECK_IN','in')
    evidence=tmp_path/'evidence';evidence.mkdir();image=evidence/'test.jpg';image.write_bytes(b'\xff\xd8synthetic')
    config=SimpleNamespace(store_id='shop',evidence_dir=str(evidence),cloud_sync=SimpleNamespace(enabled=False))
    monkeypatch.setattr(api,'store',st.engine.store);monkeypatch.setattr(api,'config',config)
    client=TestClient(api.app)
    result=client.get('/api/v2/attendance/workspace?timezone=UTC').json()
    assert result['total']==1 and result['events'][0]['activity']=='LOGIN'
    assert client.get('/api/v2/attendance/workspace?shop_id=other').status_code==403
    assert client.get('/api/v2/attendance/workspace?timezone=invalid').status_code==422
    assert client.get('/api/v2/attendance/workspace?preset=custom').status_code==422
    assert client.get('/api/v2/attendance/personnel/'+p['id']+'/state').json()['label']=='Working'
    policy=client.get('/api/v2/attendance/policy').json()['policy'];policy['allowed_break_minutes']=25
    assert client.put('/api/v2/attendance/policy',json=policy).status_code==200
    old=next(e for e in st.engine.store.person_events() if e['id']=='manual:in')
    assert json.loads(old['metadata_json'])['attendance_policy_snapshot']['values']['allowedBreakMinutes']==60
    metadata=json.loads(old['metadata_json']);metadata['snapshot_paths']=[str(image),str(tmp_path/'outside.jpg')]
    with st.engine.store._conn() as conn:conn.execute('UPDATE person_events SET metadata_json=? WHERE id=?',(json.dumps(metadata),'manual:in'))
    data=client.get('/api/v2/attendance/workspace?timezone=UTC').json()
    assert len(data['events'][0]['evidence']['images'])==1
    assert client.get(data['events'][0]['evidence']['images'][0]['url']).status_code==200
    assert client.get('/api/v2/attendance/events/manual:in/evidence?index=1').status_code==404
    assert str(tmp_path) not in json.dumps(data)


def test_local_workspace_real_chrome(station,tmp_path,monkeypatch):
    import camera_service.api as api
    from attendance_browser_support import verify_workspace_browser
    st,now,p,event=station
    for index,action in enumerate(('CHECK_IN','START_BREAK','END_BREAK','CHECK_OUT')):
        apply(st,now,action,'browser-'+str(index))
    import cv2
    import numpy as np
    images=[]
    for index in range(3):
        path=tmp_path/('synthetic-'+str(index)+'.jpg')
        assert cv2.imwrite(str(path),np.full((120,160,3),40+index*40,dtype=np.uint8))
        images.append(str(path))
    clip=tmp_path/'synthetic.webm'
    writer=cv2.VideoWriter(str(clip),cv2.VideoWriter_fourcc(*'VP80'),10,(160,120))
    assert writer.isOpened()
    for index in range(20):writer.write(np.full((120,160,3),40+index*5,dtype=np.uint8))
    writer.release()
    with st.engine.store._conn() as conn:
        row=conn.execute("SELECT metadata_json FROM person_events WHERE id='manual:browser-0'").fetchone()
        metadata=json.loads(row['metadata_json']);metadata.update(snapshot_paths=images,clip_path=str(clip),evidence_pending=False)
        conn.execute("UPDATE person_events SET metadata_json=? WHERE id='manual:browser-0'",(json.dumps(metadata),))
    monkeypatch.setattr(api,'store',st.engine.store)
    monkeypatch.setattr(api,'config',SimpleNamespace(store_id='shop',evidence_dir=str(tmp_path),cloud_sync=SimpleNamespace(enabled=False)))
    verify_workspace_browser(api.app,'/api/v2/attendance','local','Synthetic Operator')
