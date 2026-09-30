from datetime import datetime, timezone, timedelta
import pytest
from camera_service.attendance_station import AttendanceStation
from camera_service.attendance_engine import AttendanceEngine
from camera_service.storage import SQLiteStore
from camera_service.models import IdentitySeen, PersonnelCreate, PersonnelRole


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
    assert not station.engine.store.queued_events()


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
