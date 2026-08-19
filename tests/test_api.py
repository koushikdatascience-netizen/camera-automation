import importlib, os
from fastapi.testclient import TestClient

def test_personnel_api(tmp_path,monkeypatch):
 monkeypatch.chdir(tmp_path)
 import camera_service.api as api
 api.store=api.SQLiteStore(str(tmp_path/'api.db')); api.face_service.store=api.store; api.attendance_engine.store=api.store
 c=TestClient(api.app)
 r=c.post('/api/v1/personnel',json={'employee_code':'E1','full_name':'Alice','role':'WORKER'}); assert r.status_code==200
 pid=r.json()['id']; assert c.get('/api/v1/personnel').json()['items'][0]['id']==pid; assert c.get('/api/v1/attendance').status_code==200; assert c.get('/api/v1/unknown-incidents').status_code==200
