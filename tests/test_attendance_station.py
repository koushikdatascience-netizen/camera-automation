from datetime import datetime, timezone, timedelta
import pytest
from camera_service.attendance_station import AttendanceStation
from camera_service.attendance_engine import AttendanceEngine
from camera_service.storage import SQLiteStore
from camera_service.models import IdentitySeen, PersonnelCreate, PersonnelRole


@pytest.fixture(autouse=True)
def manual_attendance_mode(monkeypatch):
    # This suite verifies operator-confirmed attendance, not AUTO check-in.
    monkeypatch.setenv('CAMERA_EYE_ATTENDANCE_MODE', 'MANUAL')


def station_fixture(tmp_path):
    store = SQLiteStore(str(tmp_path / 'station.db'))
    person = store.create_person(PersonnelCreate(employee_code='E1', full_name='Alice', role=PersonnelRole.WORKER))
    engine = AttendanceEngine(store, 'shop')
    now = datetime.now(timezone.utc)
    engine.on_identity(IdentitySeen(store_id='shop', camera_id='entrance', track_id='1',
                       person_id=person['id'], timestamp=now, confidence=.9, bbox=(0, 0, 10, 10)))
    return AttendanceStation(engine), now


def test_candidate_expiry_and_lost_track(tmp_path):
    station, now = station_fixture(tmp_path)
    candidate = station.candidate('entrance', now)
    assert candidate['employee_code'] == 'E1'
    assert candidate['state'] == 'OUT'  # observation alone is not confirmation
    assert station.candidate('entrance', now + timedelta(seconds=16)) is None
    with pytest.raises(ValueError, match='expired'):
        station.apply('entrance', candidate['person_id'], candidate['token'], 'CHECK_IN', 'OUT', 'late', now + timedelta(seconds=16))
    station.engine.on_track_lost('entrance', '1')
    assert station.candidate('entrance', now) is None


def test_repeated_recognition_keeps_candidate_actionable(tmp_path):
    station, now = station_fixture(tmp_path)
    original = station.candidate('entrance', now)
    ev = station.engine.identities[('entrance', '1')]
    station.engine.on_identity(ev.model_copy(update={'timestamp': now + timedelta(seconds=2)}))
    current = station.candidate('entrance', now + timedelta(seconds=3))
    assert current['token'] == original['token']
    assert station.apply('entrance', original['person_id'], original['token'], 'CHECK_IN', 'OUT', 'refreshed', now + timedelta(seconds=3))['applied']


def test_state_actions_duplicates_and_restart(tmp_path):
    station, now = station_fixture(tmp_path)
    def apply(action, key):
        candidate = station.candidate('entrance', now)
        return station.apply('entrance', candidate['person_id'], candidate['token'], action, candidate['state'], key, now)
    candidate = station.candidate('entrance', now)
    assert apply('CHECK_IN', 'in')['applied']
    assert station.apply('entrance', candidate['person_id'], candidate['token'], 'CHECK_IN', 'OUT', 'in', now)['duplicate']
    with pytest.raises(ValueError, match='state changed'):
        station.apply('entrance', candidate['person_id'], candidate['token'], 'CHECK_IN', 'OUT', 'in2', now)
    assert station.candidate('entrance', now)['actions'] == ['CHECK_OUT', 'START_BREAK']
    apply('START_BREAK', 'break')
    assert station.candidate('entrance', now)['state'] == 'ON_BREAK'
    station.engine.store = SQLiteStore(station.engine.store.path)
    assert station.candidate('entrance', now)['actions'] == ['END_BREAK', 'CHECK_OUT']
    apply('END_BREAK', 'end')
    apply('CHECK_OUT', 'out')
    assert station.candidate('entrance', now)['state'] == 'OUT'
    recent = station.engine.store.attendance(camera_id='entrance', limit=25)
    assert len(recent) == 1
    assert recent[0]['entry_confirmed'] and recent[0]['exit_time']
    assert recent[0]['last_break_start'] and recent[0]['last_break_end']
    assert recent[0]['full_name'] == 'Alice'
    assert len(station.engine.store.person_events()) == 4
    events=station.engine.store.queued_events()
    assert [e['event_type'] for e in events]==['ATTENDANCE_ENTRY','BREAK_START','BREAK_END','ATTENDANCE_EXIT']


def test_local_station_api_and_camera_scope(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    import camera_service.api as api
    from camera_service.camera_manager import CameraManager
    station, now = station_fixture(tmp_path)
    manager = CameraManager(str(tmp_path / 'cameras.db'))
    manager.create_camera({'camera_id':'entrance','name':'Entrance','rtsp_url':'0','camera_role':'ENTRANCE_EXIT'})
    manager.create_camera({'camera_id':'general','name':'General','rtsp_url':'1'})
    monkeypatch.setattr(api, 'station', station)
    monkeypatch.setattr(api, 'attendance_engine', station.engine)
    monkeypatch.setattr(api, 'store', station.engine.store)
    monkeypatch.setattr(api, 'camera_manager', manager)
    client = TestClient(api.app)
    candidate = client.get('/api/v1/cameras/entrance/attendance-station').json()['candidate']
    body = {'person_id':candidate['person_id'], 'token':candidate['token'], 'expected_state':'OUT', 'action':'CHECK_IN', 'request_id':'test'}
    assert client.post('/api/v1/cameras/general/attendance-station/action', json=body).status_code == 404
    assert client.post('/api/v1/cameras/entrance/attendance-station/action', json=body).status_code == 200
    data = client.get('/api/v1/cameras/entrance/attendance-station').json()
    assert data['candidate']['state'] == 'IN'
    assert data['recent'][0]['employee_code'] == 'E1'
    assert data['recent'][0]['attendance_state'] == 'IN'
    assert data['recent'][0]['arrival_snapshot'] is False
    body['request_id'] = 'new'
    assert client.post('/api/v1/cameras/entrance/attendance-station/action', json=body).status_code == 409


def test_attendance_camera_stop_blocks_candidates_and_actions(tmp_path):
    from camera_service.camera_manager import CameraManager
    station, now = station_fixture(tmp_path)
    manager = CameraManager(str(tmp_path / 'cameras.db'))
    manager.create_camera({
        'camera_id':'entrance','name':'Entrance','source_type':'webcam','rtsp_url':'0',
        'camera_role':'ENTRANCE_EXIT','attendance_active':False,
        'features':{'attendance':True,'face_recognition':True,'unknown_detection':True},
    })
    station.camera_manager = manager
    assert station.candidate('entrance', now) is None
    manager.update_camera('entrance', {'attendance_active': True})
    candidate = station.candidate('entrance', now)
    assert candidate is not None
    manager.update_camera('entrance', {'attendance_active': False})
    with pytest.raises(ValueError, match='stopped'):
        station.apply('entrance', candidate['person_id'], candidate['token'], 'CHECK_IN', 'OUT', 'stopped', now)

def test_manual_cycle_receives_finished_recognition_evidence(tmp_path, monkeypatch):
    """Real SQLite, images and VP8 encoding; synthetic frames and deterministic clock."""
    import json, cv2, numpy as np
    from types import SimpleNamespace
    from camera_service.camera_manager import CameraManager
    import camera_service.camera_manager as module
    station, now=station_fixture(tmp_path);store=station.engine.store
    manager=CameraManager.__new__(CameraManager);manager.db_path=store.path
    clock=SimpleNamespace(value=100.,monotonic=lambda:clock.value,time=lambda:clock.value)
    monkeypatch.setattr(module,'time',clock)
    class InlineThread:
        def __init__(self,target,args,**kwargs):self.target,self.args=target,args
        def start(self):self.target(*self.args)
    monkeypatch.setattr(module.threading,'Thread',InlineThread)
    frame=np.zeros((64,64,3),dtype=np.uint8)
    stream={'security_clip':{'alert_id':'unrelated-security'}}
    for index,action in enumerate(['CHECK_IN','START_BREAK','END_BREAK','CHECK_OUT']):
        clock.value=100.+index*10;now+=timedelta(seconds=1)
        ev=station.engine.identities[('entrance','1')]
        station.engine.on_identity(ev.model_copy(update={'timestamp':now}))
        photo=manager._save_event_snapshot(frame,'entrance','initial')
        parent=store.add_person_event(station.candidate('entrance',now)['person_id'],'shop',
            'entrance','PERSON_RECOGNIZED',now,{'snapshot_paths':[photo],'evidence_pending':True,'evidence_status':'PENDING_CAPTURE'})
        manager._begin_attendance_evidence(stream,parent,'entrance',photo)
        candidate=station.candidate('entrance',now)
        result=station.apply('entrance',candidate['person_id'],candidate['token'],action,candidate['state'],str(index),now,evidence_path=photo)
        for delta in (.6,1.2,2.,3.2):
            clock.value=100.+index*10+delta;frame[:]=int(delta*50)
            manager._record_attendance_evidence_frame(stream,frame,store)
        metadata=json.loads(next(e for e in store.person_events() if e['id']==result['event_id'])['metadata_json'])
        assert metadata['evidence_parent_event_id']==parent
        assert metadata['evidence_status']=='COMPLETE' and metadata['evidence_pending'] is False
        assert len(set(metadata['snapshot_paths']))==3
        assert all(cv2.imread(path) is not None for path in metadata['snapshot_paths'])
        capture=cv2.VideoCapture(metadata['clip_path'])
        assert capture.isOpened() and capture.read()[0] and capture.get(cv2.CAP_PROP_FRAME_COUNT)>=3
        capture.release()
        queued=next(e for e in store.queued_events(100) if e['id']==result['event_id'])
        assert json.loads(queued['payload_json'])['metadata']['evidence_status']=='COMPLETE'
    assert stream['security_clip']['alert_id']=='unrelated-security'


def test_interrupted_capture_remains_partial(tmp_path):
    import json
    from camera_service.camera_manager import CameraManager
    station,now=station_fixture(tmp_path);store=station.engine.store
    parent=store.add_person_event(station.candidate('entrance',now)['person_id'],'shop',
        'entrance','PERSON_RECOGNIZED',now,{'evidence_pending':True})
    candidate=station.candidate('entrance',now)
    result=station.apply('entrance',candidate['person_id'],candidate['token'],'CHECK_IN','OUT','interrupted',now)
    manager=CameraManager.__new__(CameraManager);stream={}
    manager._begin_attendance_evidence(stream,parent,'entrance',None)
    manager._finish_attendance_evidence(stream,store)
    metadata=json.loads(next(e for e in store.person_events() if e['id']==result['event_id'])['metadata_json'])
    assert metadata['evidence_pending'] is False and metadata['evidence_status']!='COMPLETE'
    assert metadata['evidence_missing']['clip']=='no_frames'
    assert 'camera_stopped' in metadata['evidence_missing']['snapshots']
