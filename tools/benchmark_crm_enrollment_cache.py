"""Offline cache benchmark. Simulated inference; never measures production model CPU."""
import ast
import base64
import json
import os
from pathlib import Path
import subprocess
import time
import uuid
from types import SimpleNamespace
import cv2
import numpy as np

root=Path('artifacts')/('enrollment-benchmark-'+uuid.uuid4().hex[:8]);root.mkdir(parents=True)
os.environ.pop('SNAPKEY_DATABASE_URL',None)
os.environ['SNAPKEY_ENV']='development'
os.environ['SNAPKEY_PORTAL_DB']=str(root/'bootstrap.db')
from cloud_portal import api
from cloud_portal.storage import PortalStore
baseline=subprocess.check_output(['git','show','d88d170:cloud_portal/api.py'],text=True)
node=next(n for n in ast.parse(baseline).body if isinstance(n,ast.FunctionDef) and n.name=='_refresh_crm_personnel_locked')
node.name='_baseline_refresh'
exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])), '<baseline>', 'exec'),api.__dict__)
_,encoded=cv2.imencode('.png',np.full((64,64,3),120,np.uint8))
source=base64.b64encode(encoded).decode()
users=[{'id':f'test-{i}','tenantId':'test-uuid','tenantCode':'test','name':f'Test {i}', 'faceImages':[source], 'faceEmbeddings':None} for i in range(4)]
api.crm_client=SimpleNamespace(face_attendance_configured=True,face_embeddings=lambda _:users)
api.enrollment_model_key=lambda:'synthetic-model-v1'
results={}
for label,operation in [('baseline',api._baseline_refresh),('persistent',api._refresh_crm_personnel_locked)]:
 api.store=PortalStore(str(root/(label+'.db')))
 state={'inferences':0,'model_initializations':0,'model':None}
 class FakeModel:
  def enroll(self,_):
   state['inferences']+=1
   deadline=time.perf_counter()+.005
   while time.perf_counter()<deadline: pass
   return [1.]+[0.]*511,.9
 def enroller():
  if state['model'] is None:state['model_initializations']+=1;state['model']=FakeModel()
  return state['model']
 api._cloud_face_enroller=enroller
 started=time.perf_counter();cpu=time.process_time();latencies=[]
 for restart in range(3):
  state['model']=None;api._crm_face_image_fingerprints=set()
  for refresh in range(2):
   before=time.perf_counter();operation('test','test-shop');latencies.append(time.perf_counter()-before)
 results[label]={'inference_count':state['inferences'],'model_initialization_count':state['model_initializations'],
  'wall_seconds':round(time.perf_counter()-started,4),'cpu_seconds':round(time.process_time()-cpu,4),
  'refresh_latency_median_ms':round(float(np.median(latencies))*1000,3)}
results['scope']='Synthetic 4 users, 6 refreshes, 3 simulated process restarts; real SQLite/decode, mocked 5ms inference. No real-model or VPS CPU measurement.'
results['cache_hit_ratio']=round(1-results['persistent']['inference_count']/24,4)
from fastapi.testclient import TestClient
principal=api.PortalPrincipal('benchmark','test','test','test-shop','test-user','Test User','ADMIN')
api.app.dependency_overrides[api.require_portal_session]=lambda:principal
client=TestClient(api.app)
async_refresh=api._refresh_crm_personnel
for label in ('baseline','background'):
 api._refresh_crm_personnel=(lambda tenant,shop:api._baseline_refresh(tenant,shop)) if label=='baseline' else async_refresh
 latencies=[]
 for _ in range(3):
  api._crm_face_image_fingerprints=set();api._crm_personnel_last_refresh.clear()
  started=time.perf_counter()
  response=client.get('/portal/v1/tenants/test/personnel')
  assert response.status_code==200
  latencies.append((time.perf_counter()-started)*1000)
 results[label+'_http_median_ms']=round(float(np.median(latencies)),3)
api._crm_enrollment_worker.shutdown()
results['http_scope']='Actual FastAPI TestClient GET against isolated mirror, test principal, synthetic CRM/images; forced refresh requests. Not production network latency.'
(root/'results.json').write_text(json.dumps(results,indent=2))
print(json.dumps(results,indent=2));print('Artifact:',root/'results.json')
