import importlib, os
from fastapi.testclient import TestClient

def test_personnel_api(tmp_path,monkeypatch):
 monkeypatch.chdir(tmp_path)
 import camera_service.api as api
 api.store=api.SQLiteStore(str(tmp_path/'api.db')); api.face_service=None; api.attendance_engine.store=api.store
 c=TestClient(api.app)
 r=c.post('/api/v1/personnel',json={'employee_code':'E1','full_name':'Alice','role':'WORKER'}); assert r.status_code==200
 pid=r.json()['id']; assert c.get('/api/v1/personnel').json()['items'][0]['id']==pid; assert c.get('/api/v1/attendance').status_code==200; assert c.get('/api/v1/unknown-incidents').status_code==200
 status=c.get('/api/v1/object-security/status').json(); assert status['message']=='No custom scissors model installed.' and status['ready'] is False
 bad=c.post('/api/v1/object-security/models/upload',files={'file':('bad.txt',b'not a model','text/plain')},data={'object_class':'scissors'}); assert bad.status_code==400
def test_license_blocks_camera_limit_and_unlicensed_features(tmp_path,monkeypatch):
 monkeypatch.chdir(tmp_path)
 import camera_service.api as api
 class FakeStatus:
  active=True; limited_mode=False; max_cameras=1; plan='basic'
  def allows_feature(self,feature): return feature=='tracking'
  def model_dump(self): return {'active':True,'limited_mode':False,'max_cameras':1,'features':['tracking']}
 class FakeLicense:
  def status(self): return FakeStatus()
 api.camera_manager=api.CameraManager(str(tmp_path/'cameras.db')); api.license_manager=FakeLicense()
 c=TestClient(api.app)
 first=c.post('/api/v1/cameras',json={'camera_id':'cam1','name':'Camera 1','rtsp_url':'0','features':{}})
 assert first.status_code==200
 second=c.post('/api/v1/cameras',json={'camera_id':'cam2','name':'Camera 2','rtsp_url':'1','features':{}})
 assert second.status_code==402
 api.camera_manager=api.CameraManager(str(tmp_path/'feature.db'))
 premium=c.post('/api/v1/cameras',json={'camera_id':'cam3','name':'Camera 3','rtsp_url':'2','features':{'object_security':True}})
 assert premium.status_code==402
