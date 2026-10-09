import threading
import time
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np

from camera_service.camera.supervisor import CameraSupervisor
from camera_service.camera_manager import CameraConfig, CameraManager


def wait_until(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("Timed out waiting for runtime")


def test_two_viewers_share_one_pipeline_and_closing_view_does_not_stop_ai(tmp_path):
    manager = CameraManager(str(tmp_path / 'cameras.db'))
    manager.create_camera({'camera_id': 'cam1', 'name': 'Entrance', 'rtsp_url': '0'})
    calls = []
    def pipeline(camera, stop, publish):
        calls.append(camera.camera_id)
        while not stop.is_set():
            publish(b'raw', b'annotated')
            yield b'packet'
            stop.wait(0.02)
    supervisor = CameraSupervisor(None, None, None, None, manager, pipeline=pipeline)
    supervisor.start()
    try:
        wait_until(lambda: supervisor.snapshot('cam1') == b'raw')
        first = supervisor.stream('cam1', overlay=False)
        second = supervisor.stream('cam1', overlay=True)
        assert b'raw' in next(first)
        assert b'annotated' in next(second)
        first.close(); second.close()
        assert calls == ['cam1']
        assert supervisor.threads['cam1'].is_alive()
        manager.update_camera('cam1', {'enabled': False})
        supervisor.reconcile()
        assert not supervisor.threads.get('cam1')
    finally:
        supervisor.shutdown()


def test_cloud_reapply_does_not_change_version_or_erase_local_features(tmp_path):
    manager = CameraManager(str(tmp_path / 'cameras.db'))
    local = manager.create_camera({'camera_id': 'cam1', 'name': 'Entrance', 'rtsp_url': '0',
                                  'source_type': 'webcam', 'features': {'attendance': True}})
    assignment = {'camera_id': 'cam1', 'name': 'Entrance', 'source': '0', 'source_type': 'webcam',
                  'features': {'face_recognition': True}, 'settings': {'tracking_mode': 'track'}}
    first = manager.apply_cloud_camera(assignment)
    second = manager.apply_cloud_camera(assignment)
    assert first.features.attendance is True
    assert first.tracking_mode == 'track'
    assert first.updated_at == second.updated_at


def test_runtime_status_expires_when_frames_stop(tmp_path):
    manager = CameraManager(str(tmp_path / 'cameras.db'))
    manager.create_camera({'camera_id': 'cam1', 'name': 'Camera', 'rtsp_url': '0'})
    manager.update_camera_status('cam1', state='ONLINE', online=True,
        last_frame_at=(datetime.now(timezone.utc) - timedelta(seconds=20)).isoformat())
    assert manager.get_camera_status('cam1').online is False


def test_face_failure_preserves_person_boxes_and_track_contract(tmp_path, monkeypatch):
    monkeypatch.setenv('SNAPKEY_INFERENCE_ROUTER_ENABLED', '1')
    manager = CameraManager(str(tmp_path / 'cameras.db'))
    calls = []
    class Backend:
        def infer(self, frame, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(detections=[SimpleNamespace(class_name='person', attributes={'class_id': 0},
                confidence=0.9, bbox_xyxy=(10, 10, 100, 180), track_id=7 if kwargs['tracking'] else None)],
                backend_type=SimpleNamespace(value='LOCAL_CPU'), inference_latency_ms=1)
    class BrokenFace:
        def detect(self, frame):
            raise RuntimeError('face provider unavailable')
    camera = CameraConfig(camera_id='cam1', name='Camera', rtsp_url='0', tracking_mode='track',
                          features={'face_recognition': True})
    manager._inference_backends[('fake.pt', 'cam1')] = Backend()
    state = {}
    recognition = SimpleNamespace(enabled=True, known_recheck_seconds=2, minimum_face_quality=.2, known_threshold=.5)
    attendance = SimpleNamespace(on_track_lost=lambda *args: None)
    manager._annotate_tracking_frame(np.zeros((240, 320, 3), np.uint8), 'fake.pt', BrokenFace(), recognition,
                                     camera, attendance, stream_state=state)
    assert state['latest_summary']['people'] == 1
    assert state['latest_overlays']
    assert calls[-1]['tracking'] is True
    camera.tracking_mode = 'detect'
    manager._annotate_tracking_frame(np.zeros((240, 320, 3), np.uint8), 'fake.pt', camera_config=camera, stream_state=state)
    assert calls[-1]['tracking'] is False
    assert all('person #1' not in overlay['text'] for overlay in state['latest_overlays'])


def test_local_api_rejects_foreign_origin_and_rebinding_host():
    from fastapi.testclient import TestClient
    from camera_service.api import app
    client = TestClient(app)
    assert client.post('/api/v1/cameras/test', json={'rtsp_url': '0'}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.get('/health', headers={'Host': 'evil.example'}).status_code == 403


def test_broken_camera_retries_without_stopping_healthy_camera(tmp_path):
    manager = CameraManager(str(tmp_path / 'cameras.db'))
    for camera_id in ('broken', 'healthy'):
        manager.create_camera({'camera_id': camera_id, 'name': camera_id, 'rtsp_url': camera_id})
    def pipeline(camera, stop, publish):
        if camera.camera_id == 'broken':
            raise RuntimeError('Disconnected')
        while not stop.is_set():
            publish(b'frame', b'boxes'); yield b'packet'; stop.wait(.02)
    supervisor = CameraSupervisor(None, None, None, None, manager, pipeline=pipeline)
    supervisor.start()
    try:
        wait_until(lambda: supervisor.snapshot('healthy') == b'frame')
        wait_until(lambda: manager.get_camera_status('broken').reconnect_count >= 1)
        assert supervisor.threads['healthy'].is_alive()
        assert not manager.get_camera_status('broken').online
    finally:
        supervisor.shutdown()


def test_legacy_quality_normalized_and_cloud_delete_stops_runtime(tmp_path):
    from camera_service.sync_worker import EdgeSyncWorker
    manager = CameraManager(str(tmp_path / 'cameras.db'))
    result = manager.apply_cloud_camera({'camera_id': 'cam', 'source': '0', 'settings': {'tracking_quality': 'balanced'}})
    assert result.tracking_quality == 65
    worker = EdgeSyncWorker(None, None, None, None, None, camera_manager=manager)
    assert worker._execute_command({'command_type': 'CAMERA_DELETE', 'request': {'camera_id': 'cam'}})['deleted']
    assert manager.get_camera('cam') is None


def test_invalid_rtsp_is_bounded_and_diagnostics_hide_credentials(tmp_path):
    manager = CameraManager(str(tmp_path / 'cameras.db'))
    started = time.monotonic()
    result = manager.test_rtsp_connection('rtsp://user:password@192.0.2.1:554/nonexistent')
    assert time.monotonic() - started < 12
    assert not result['success']
    assert 'password' not in str(result)


def test_license_corruption_and_frozen_unlicensed_mode(tmp_path, monkeypatch):
    import sys
    from camera_service.config import EdgeConfig
    from camera_service.licensing import LicenseManager
    path = tmp_path / 'license.json'
    path.write_text('{broken', encoding='utf-8')
    manager = LicenseManager(EdgeConfig(activation_required=True, license_cache_path=str(path)))
    assert manager.status().limited_mode
    manager.edge.activation_required = False
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    assert manager.status().limited_mode


def test_shop_filter_applied_before_event_limit(tmp_path):
    from cloud_portal.storage import PortalStore
    store = PortalStore(str(tmp_path / 'portal.db'))
    for event_id, shop, stamp in [('wanted','shop1','2026-09-28T10:00:00Z'), ('other','shop2','2026-09-29T10:00:00Z')]:
        store.ingest_event({'tenant_id':'tenant', 'shop_id':shop, 'site_id':'site-'+shop, 'edge_id':'edge-'+shop,
            'event_id':event_id, 'event_time':stamp, 'event_type':'SECURITY_OBJECT_ALERT', 'payload':{}})
    assert store.list_events('tenant', limit=1, shop_id='shop1')[0]['id'] == 'wanted'
    assert store.get_event('tenant','shop2','wanted') is None


def test_production_rejects_global_activation_and_untrusted_registration(tmp_path, monkeypatch):
    import cloud_portal.api as api
    from cloud_portal.storage import PortalStore
    from fastapi.testclient import TestClient
    monkeypatch.setattr(api, 'store', PortalStore(str(tmp_path / 'portal.db')))
    monkeypatch.setenv('SNAPKEY_ENV','production')
    monkeypatch.setenv('SNAPKEY_EDGE_ACTIVATION_CODE','global-secret')
    client = TestClient(api.app)
    result = client.post('/edge/v1/activate', json={'company_code':'other', 'shop_code':'other', 'machine_code':'machine', 'activation_code':'global-secret'})
    assert result.status_code == 401
    result = client.post('/auth/register', json={'company_code':'other', 'shop_code':'other', 'email':'x@example.com', 'password':'password123', 'display_name':'X', 'activation_code':'global-secret'})
    assert result.status_code == 403


def test_exported_model_failure_falls_back_offline(tmp_path, monkeypatch):
    from camera_service.inference.ultralytics_adapter import UltralyticsCPUBackend
    exported = tmp_path / 'yolo26n.onnx'; exported.touch()
    fallback = tmp_path / 'yolo26n.pt'; fallback.touch()
    backend = UltralyticsCPUBackend(exported)
    calls = []
    def fake_inference(self, frame, **kwargs):
        calls.append(self.model_path)
        if self.model_path.endswith('.onnx'):
            raise ValueError('Incompatible export')
        return 'validated'
    monkeypatch.setattr(UltralyticsCPUBackend, '_infer_once', fake_inference)
    assert backend.infer(None) == 'validated'
    assert calls == [str(exported),str(fallback)]
    assert backend.fallback_reason


def test_signed_license_grace_and_wrong_machine_rejected(tmp_path):
    import pytest
    from camera_service.config import EdgeConfig
    from camera_service.licensing import LicenseManager, generate_license_keypair, sign_license_payload
    keys=generate_license_keypair()
    manager=LicenseManager(EdgeConfig(activation_required=True, license_public_key=keys['public_key'],license_cache_path=str(tmp_path/'license.json')))
    now=datetime.now(timezone.utc)
    payload={'tenant_id':manager.edge.tenant_id,'site_id':manager.edge.site_id,'edge_id':manager.edge.edge_id,
        'machine_code':manager.machine_code(),'expires_at':(now-timedelta(days=1)).isoformat(),
        'grace_until':(now+timedelta(days=1)).isoformat(),'features':['tracking'],'max_cameras':2}
    assert manager.install_signed_license(payload,sign_license_payload(payload,keys['private_key'])).mode == 'grace'
    wrong={**payload,'machine_code':'another-machine'}
    with pytest.raises(ValueError,match='different machine'):
        manager.install_signed_license(wrong,sign_license_payload(wrong,keys['private_key']))
    assert manager.status().mode == 'grace'


def test_event_evidence_is_shop_scoped_and_cannot_escape_root(tmp_path, monkeypatch):
    import cloud_portal.api as api
    from cloud_portal.storage import PortalStore
    from fastapi.testclient import TestClient
    from urllib.parse import urlparse, parse_qs
    def portal_auth(client, monkeypatch, shop='shop1'):
        monkeypatch.setenv('SNAPKEY_CRM_INTEGRATION_KEY','isolated-evidence-test')
        result=client.post('/crm/session',json={'tenantId':'tenant-a','shopCode':shop,'role':'OWNER'},headers={'X-CRM-Integration-Key':'isolated-evidence-test'})
        return {'Authorization':'Bearer '+parse_qs(urlparse(result.json()['launchUrl']).fragment)['session'][0]}
    monkeypatch.setattr(api,'store',PortalStore(str(tmp_path/'portal.db')))
    monkeypatch.setenv('SNAPKEY_EVIDENCE_ROOT',str(tmp_path/'evidence'))
    client=TestClient(api.app)
    auth=portal_auth(client,monkeypatch)
    path=tmp_path/'evidence'/'tenant-a'/'shop1'/'edge-1'/'event.jpg'
    path.parent.mkdir(parents=True);path.write_bytes(b'JPEG')
    event={'tenant_id':'tenant-a','shop_id':'shop1','site_id':'site-1','edge_id':'edge-1',
        'event_id':'event','event_time':'2026-09-29T10:00:00Z','event_type':'SECURITY_OBJECT_ALERT',
        'payload':{'metadata':{'cloud_evidence':{'evidence_id':'tenant-a/shop1/edge-1/event.jpg'}}}}
    api.store.ingest_event(event)
    assert client.get('/portal/v1/tenants/tenant-a/events/event/evidence',headers=auth).content==b'JPEG'
    foreign=portal_auth(client,monkeypatch,shop='shop2')
    assert client.get('/portal/v1/tenants/tenant-a/events/event/evidence',headers=foreign).status_code==404
    assert client.get('/portal/v1/tenants/tenant-b/events/event/evidence',headers=auth).status_code==403


def test_recognition_uses_current_snapshot_for_initial_arrival(monkeypatch):
    from camera_service.attendance_engine import AttendanceEngine
    from camera_service.models import IdentitySeen
    monkeypatch.setenv('CAMERA_EYE_ATTENDANCE_MODE', 'AUTO')
    calls=[]
    class Store:
        def get_person(self, person_id):
            return {'attendance_mode':'AUTO'}
        def create_arrival(self, *args, **kwargs):
            calls.append((args, kwargs))
            return {'id':'session'},True
    engine=AttendanceEngine(Store(),'store')
    now=datetime.now(timezone.utc)
    engine.on_identity(IdentitySeen(store_id='store',camera_id='cam',track_id='1',person_id='person',timestamp=now,
        confidence=.9,bbox=(0,0,10,10),snapshot_path='recognition.jpg'))
    assert calls[-1][0][-1]=='recognition.jpg'
    assert calls[-1][1]['confirmed'] is True


def test_cloud_event_sync_strips_local_evidence_paths_without_losing_event(tmp_path):
    from camera_service.sync_worker import EdgeSyncWorker
    photo=tmp_path/'evidence.jpg';photo.write_bytes(b'jpeg')
    events=[]
    class Store:
        def queued_events(self, limit):
            return [{'id':'evt-1','payload_json':json.dumps({'event_id':'evt-1','event_type':'SECURITY_OBJECT_ALERT',
                'metadata':{'snapshot_path':str(photo),'clip_path':'C:/private/alert.mp4'}}),'attempts':0}]
        def mark_event_synced(self, event_id): events.append(event_id)
        def now(self): return 'now'
    class Cloud:
        def enabled(self): return True
        def heartbeat(self, edge, status): return {'received_at':'now'}
        def edge_commands(self): return []
        def camera_config(self): return {'items':[]}
        def personnel_config(self): return {'items':[]}
        def upload_event_evidence(self, event_id, path): return {'evidence_id':'tenant/shop/edge/evt-1.jpg'}
        def post_event(self, edge, event): events.append(event)
    class License:
        def status(self): return SimpleNamespace(active=True,allows_feature=lambda name: True,model_dump=lambda:{})
    worker=EdgeSyncWorker(Store(),Cloud(),SimpleNamespace(),SimpleNamespace(batch_size=10),License())
    assert worker.run_once().synced==1
    assert events[-1]=='evt-1'
    metadata=events[0]['metadata']
    assert metadata['cloud_evidence']['evidence_id']=='tenant/shop/edge/evt-1.jpg'
    assert 'snapshot_path' not in metadata and 'clip_path' not in metadata
