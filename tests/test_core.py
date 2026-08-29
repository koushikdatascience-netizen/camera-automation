from datetime import datetime,timezone,timedelta
import numpy as np
from camera_service.bbox_utils import normalize_xyxy,width,height,area,crop
from camera_service.config import RecognitionConfig,AttendanceLine
from camera_service.identity_engine import IdentityResolutionEngine
from camera_service.line_crossing import LineCrossingDetector
from camera_service.storage import SQLiteStore
from camera_service.attendance_engine import AttendanceEngine
from camera_service.models import PersonnelCreate,PersonnelRole,IdentitySeen,LineCrossingEvent
from camera_service.object_security.alerts import ObjectSecurityAlerter
from camera_service.object_security.confirmation import TemporalConfirmation
from camera_service.object_security.model_registry import ObjectSecurityModelRegistry
from camera_service.object_security.roi import map_box_from_offset
from camera_service.object_security.tiling import suppress_duplicates
from camera_service.config import AppConfig
from camera_service.licensing import LicenseManager, generate_license_keypair, sign_license_payload
from camera_service.sync_worker import EdgeSyncWorker

def test_bbox():
 b=normalize_xyxy((10,20,30,50)); assert width(b)==20 and height(b)==30 and area(b)==600; assert crop(np.zeros((100,100,3),dtype=np.uint8),b).shape[:2]==(30,20)
def test_identity_consensus_and_unknown():
 cfg=RecognitionConfig(required_known_observations=2,max_recognition_attempts=2,unknown_confirmation_seconds=0)
 e=IdentityResolutionEngine(cfg); t=datetime.now(timezone.utc)
 assert e.observe('c','1',t,'p',.8).state=='UNRESOLVED'; assert e.observe('c','1',t,'p',.82).state=='KNOWN'
 assert e.observe('c','2',t,None,.2).state=='UNRESOLVED'; assert e.observe('c','2',t+timedelta(seconds=1),None,.1).state=='UNKNOWN'
def test_line_crossing():
 l=LineCrossingDetector(AttendanceLine(x1=0,y1=50,x2=100,y2=50,inside_side='positive',min_crossing_displacement_px=5)); assert l.update('1',(10,10,20,30)) is None; assert l.update('1',(10,40,20,70))=='ENTRY'
def test_lost_track_does_not_exit(tmp_path):
 s=SQLiteStore(str(tmp_path/'db.sqlite')); p=s.create_person(PersonnelCreate(employee_code='E1',full_name='A',role=PersonnelRole.WORKER)); a=AttendanceEngine(s,'store'); t=datetime.now(timezone.utc)
 ident=IdentitySeen(store_id='store',camera_id='cam',track_id='1',person_id=p['id'],timestamp=t,confidence=.9,bbox=(0,0,10,20)); cross=LineCrossingEvent(store_id='store',camera_id='cam',track_id='1',direction='ENTRY',timestamp=t,bbox=(0,0,10,20)); a.on_identity(ident); a.on_crossing(cross); assert s.open_session(p['id'],'store'); a.on_track_lost('cam','1'); assert s.open_session(p['id'],'store')
def test_duplicate_arrival_and_exit(tmp_path):
 s=SQLiteStore(str(tmp_path/'db.sqlite')); p=s.create_person(PersonnelCreate(employee_code='E1',full_name='A',role=PersonnelRole.WORKER)); a=AttendanceEngine(s,'store'); t=datetime.now(timezone.utc)
 i=IdentitySeen(store_id='store',camera_id='cam',track_id='1',person_id=p['id'],timestamp=t,confidence=.9,bbox=(0,0,10,20)); a.on_identity(i); a.on_crossing(LineCrossingEvent(store_id='store',camera_id='cam',track_id='1',direction='ENTRY',timestamp=t,bbox=(0,0,10,20))); a.on_crossing(LineCrossingEvent(store_id='store',camera_id='cam',track_id='1',direction='ENTRY',timestamp=t,bbox=(0,0,10,20))); assert len(s.attendance(p['id']))==1; te=t+timedelta(hours=1); a.on_identity(i.model_copy(update={'timestamp':te})); a.on_crossing(LineCrossingEvent(store_id='store',camera_id='cam',track_id='1',direction='EXIT',timestamp=te,bbox=(0,0,10,20))); assert s.attendance(p['id'])[0]['status']=='CLOSED'
def test_entry_exit_times_and_snapshots_are_stored(tmp_path):
 s=SQLiteStore(str(tmp_path/'db.sqlite')); p=s.create_person(PersonnelCreate(employee_code='E2',full_name='B',role=PersonnelRole.WORKER)); a=AttendanceEngine(s,'store'); first_seen=datetime(2026,8,24,8,59,55,tzinfo=timezone.utc); entry=first_seen+timedelta(seconds=5); exit_time=entry+timedelta(hours=8)
 entry_seen=IdentitySeen(store_id='store',camera_id='entrance',track_id='11',person_id=p['id'],timestamp=first_seen,confidence=.91,bbox=(0,0,10,20),snapshot_path='data/evidence/entrance/first_seen.jpg')
 exit_seen=entry_seen.model_copy(update={'timestamp':exit_time,'snapshot_path':'data/evidence/entrance/exit.jpg'})
 a.on_identity(entry_seen); a.on_crossing(LineCrossingEvent(store_id='store',camera_id='entrance',track_id='11',direction='ENTRY',timestamp=entry,bbox=(0,0,10,20)))
 a.on_crossing(LineCrossingEvent(store_id='store',camera_id='entrance',track_id='11',direction='ENTRY',timestamp=entry+timedelta(minutes=1),bbox=(0,0,10,20)))
 a.on_identity(exit_seen); a.on_crossing(LineCrossingEvent(store_id='store',camera_id='entrance',track_id='11',direction='EXIT',timestamp=exit_time,bbox=(0,0,10,20)))
 row=s.attendance(p['id'])[0]
 assert row['arrival_time']==entry.isoformat() and row['exit_time']==exit_time.isoformat()
 assert row['arrival_camera']=='entrance' and row['exit_camera']=='entrance'
 assert row['arrival_snapshot']=='data/evidence/entrance/first_seen.jpg' and row['exit_snapshot']=='data/evidence/entrance/exit.jpg'
 assert row['arrival_confidence']==.91 and row['exit_confidence']==.91 and row['status']=='CLOSED'
def test_security_alert_history_snapshot_clip_and_ack(tmp_path):
 s=SQLiteStore(str(tmp_path/'db.sqlite')); t=datetime(2026,8,24,10,0,0,tzinfo=timezone.utc)
 alert=s.create_security_alert('store','jewel_cam','SECURITY_OBJECT_ALERT','scissors',.77,t,snapshot_path='snap.jpg',metadata={'bbox':[1,2,3,4]})
 assert alert['status']=='OPEN' and alert['object_label']=='scissors' and alert['snapshot_path']=='snap.jpg'
 updated=s.update_security_alert_clip(alert['id'],'clip.mp4')
 assert updated['clip_path']=='clip.mp4' and s.security_alerts()[0]['id']==alert['id']
 ack=s.acknowledge_security_alert(alert['id'])
 assert ack['status']=='ACKNOWLEDGED' and ack['acknowledged_at']
def test_object_security_registry_activation_and_rollback(tmp_path):
 r=ObjectSecurityModelRegistry(tmp_path/'models'); assert r.list_models('scissors')['missing_production'] is True
 m1=tmp_path/'m1.pt'; m1.write_bytes(b'fake-pt-one'); m2=tmp_path/'m2.pt'; m2.write_bytes(b'fake-pt-two')
 c1=r.create_candidate('scissors',m1,{'version':'v1','source':'Kaggle'}); c2=r.create_candidate('scissors',m2,{'version':'v2','source':'Kaggle'})
 validate=lambda path: {'ok':True,'path':str(path)}
 models=r.activate_candidate('scissors',c1['id'],validate); assert models['active']['metadata']['version']=='v1' and models['previous'] is None
 models=r.activate_candidate('scissors',c2['id'],validate); assert models['active']['metadata']['version']=='v2' and models['previous']['metadata']['version']=='v1'
 models=r.rollback('scissors',validate); assert models['active']['metadata']['version']=='v1'
 assert r.delete_candidate('scissors',c2['id']) is True
def test_object_security_confirmation_cooldown_roi_tiling_and_events(tmp_path):
 c=TemporalConfirmation(window_frames=5,required_hits=3,minimum_confidence=.3); assert c.update('1',.31)=='VERIFYING'; assert c.update('1',.2)=='VERIFYING'; assert c.update('1',.4)=='VERIFYING'; assert c.update('1',.5)=='CONFIRMED'
 assert map_box_from_offset((1,2,3,4),(10,20))==(11.0,22.0,13.0,24.0)
 detections=[{'bbox':(0,0,10,10),'confidence':.9},{'bbox':(1,1,11,11),'confidence':.8},{'bbox':(40,40,50,50),'confidence':.7}]
 assert len(suppress_duplicates(detections,.4))==2
 a=ObjectSecurityAlerter(); assert a.should_alert('scissors',15) is True and a.should_alert('scissors',15) is False
 s=SQLiteStore(str(tmp_path/'events.db')); row=s.create_object_security_event('cam','scissors',.82,datetime(2026,8,26,10,0,0,tzinfo=timezone.utc),track_id='1',confirmed=True,alert_sent=True,snapshot_path='snap.jpg',model_version='v1')
 assert row['object_class']=='scissors' and row['confirmed']==1 and s.object_security_events()[0]['id']==row['id']
def test_edge_queue_contains_attendance_and_security_payloads(tmp_path):
 s=SQLiteStore(str(tmp_path/'sync.db')); p=s.create_person(PersonnelCreate(employee_code='E9',full_name='Edge Worker',role=PersonnelRole.WORKER)); t=datetime(2026,8,28,9,0,0,tzinfo=timezone.utc)
 s.create_arrival(p['id'],'store-a',t,'door',.93,'entry.jpg',confirmed=True)
 s.close_exit(p['id'],'store-a',t+timedelta(hours=1),'door',.91,'exit.jpg')
 s.create_security_alert('store-a','jewel','SECURITY_OBJECT_ALERT','scissors',.88,t,snapshot_path='snap.jpg',clip_path='clip.mp4',metadata={'bbox':[1,2,3,4]})
 events=s.queued_events(10)
 assert {e['event_type'] for e in events} >= {'ATTENDANCE_ENTRY','ATTENDANCE_EXIT','SECURITY_OBJECT_ALERT'}
 security_payload=[e for e in events if e['event_type']=='SECURITY_OBJECT_ALERT'][0]['payload_json']
 assert '"object_label": "scissors"' in security_payload and '"clip_path": "clip.mp4"' in security_payload
def test_edge_sync_worker_marks_success_and_failure(tmp_path):
 class FakeCloud:
  def __init__(self,fail=False): self.fail=fail; self.sent=[]
  def enabled(self): return True
  def post_event(self,edge,event):
   if self.fail: raise RuntimeError('offline')
   self.sent.append((edge.edge_id,event['event_id']))
 cfg=AppConfig(); s=SQLiteStore(str(tmp_path/'worker.db')); t=datetime(2026,8,28,9,0,0,tzinfo=timezone.utc)
 s.add_person_event(None,'store-a','cam','CROWD_ALERT',t,{'person_count':21})
 ok_cloud=FakeCloud(); worker=EdgeSyncWorker(s,ok_cloud,cfg.edge,cfg.cloud_sync,LicenseManager(cfg.edge))
 result=worker.run_once()
 assert result.synced==1 and result.failed==0 and s.event_queue_status()['counts']['SYNCED']==1
 s.add_person_event(None,'store-a','cam','CROWD_ALERT',t,{'person_count':22})
 fail_worker=EdgeSyncWorker(s,FakeCloud(fail=True),cfg.edge,cfg.cloud_sync,LicenseManager(cfg.edge))
 result=fail_worker.run_once()
 assert result.failed==1 and s.event_queue_status()['recent_errors'][0]['last_error']=='offline'
def test_signed_license_cache_expiry_grace_features_and_machine_binding(tmp_path, monkeypatch):
 keys=generate_license_keypair()
 cfg=AppConfig()
 cfg.edge.activation_required=True; cfg.edge.license_cache_path=str(tmp_path/'license.json'); cfg.edge.edge_id='edge-a'; cfg.edge.tenant_id='tenant-a'; cfg.edge.site_id='site-a'; cfg.edge.license_public_key=keys['public_key']
 manager=LicenseManager(cfg.edge)
 payload={'tenant_id':'tenant-a','site_id':'site-a','edge_id':'edge-a','machine_code':manager.machine_code(),'plan':'pro','max_cameras':7,'features':['tracking','attendance','object_security'],'expires_at':'2099-01-01T00:00:00+00:00','grace_until':'2099-01-08T00:00:00+00:00'}
 status=manager.install_signed_license(payload,sign_license_payload(payload,keys['private_key']))
 assert status.active is True and status.max_cameras==7 and status.allows_feature('object_security') and not status.allows_feature('cloud_sync')
 expired={**payload,'expires_at':'2020-01-01T00:00:00+00:00','grace_until':'2020-01-08T00:00:00+00:00'}
 cfg.edge.license_cache_path=str(tmp_path/'expired.json'); manager=LicenseManager(cfg.edge); manager.install_signed_license(expired,sign_license_payload(expired,keys['private_key']))
 expired_status=manager.status()
 assert expired_status.active is False and expired_status.limited_mode is True and expired_status.mode=='expired'
