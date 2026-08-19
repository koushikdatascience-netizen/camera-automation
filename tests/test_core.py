from datetime import datetime,timezone,timedelta
import numpy as np
from camera_service.bbox_utils import normalize_xyxy,width,height,area,crop
from camera_service.config import RecognitionConfig,AttendanceLine
from camera_service.identity_engine import IdentityResolutionEngine
from camera_service.line_crossing import LineCrossingDetector
from camera_service.storage import SQLiteStore
from camera_service.attendance_engine import AttendanceEngine
from camera_service.models import PersonnelCreate,PersonnelRole,IdentitySeen,LineCrossingEvent

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
