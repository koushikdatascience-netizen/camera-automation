from fastapi.testclient import TestClient


def test_cloud_portal_ingests_events_by_tenant(tmp_path, monkeypatch):
 monkeypatch.setenv('SNAPKEY_PORTAL_DB', str(tmp_path/'portal.db'))
 monkeypatch.setenv('SNAPKEY_EDGE_API_TOKEN', 'test-edge-token')
 monkeypatch.setenv('SNAPKEY_ENV', 'development')
 import importlib
 import cloud_portal.api as api
 importlib.reload(api)
 client=TestClient(api.app)
 envelope={'schema_version':'edge.event.v1','tenant_id':'tenant-a','site_id':'site-1','edge_id':'edge-1','event_id':'evt-1','event_type':'SECURITY_OBJECT_ALERT','event_time':'2026-08-28T09:00:00+00:00','store_id':'store-a','camera_id':'cam-1','payload':{'event_id':'evt-1','metadata':{'object_label':'scissors'}}}
 assert client.post('/edge/v1/events',json=envelope,headers={'Authorization':'Bearer test-edge-token'}).status_code==200
 assert client.get('/portal/v1/tenants/tenant-a/summary').json()['events']['SECURITY_OBJECT_ALERT']==1
 items=client.get('/portal/v1/tenants/tenant-a/events',params={'site_id':'site-1'}).json()['items']
 assert items[0]['tenant_id']=='tenant-a' and items[0]['payload']['payload']['metadata']['object_label']=='scissors'
def test_cloud_portal_issues_signed_license(tmp_path, monkeypatch):
 monkeypatch.setenv('SNAPKEY_PORTAL_DB', str(tmp_path/'portal.db'))
 from camera_service.licensing import generate_license_keypair
 keys=generate_license_keypair()
 monkeypatch.setenv('SNAPKEY_LICENSE_PRIVATE_KEY', keys['private_key'])
 import importlib
 import cloud_portal.api as api
 importlib.reload(api)
 client=TestClient(api.app)
 response=client.post('/portal/v1/licenses/issue',json={'tenant_id':'tenant-a','site_id':'site-1','edge_id':'edge-1','machine_code':'ABC','max_cameras':3,'features':['tracking','cloud_sync'],'days':30,'grace_days':7})
 assert response.status_code==200
 data=response.json()
 assert data['license']['max_cameras']==3 and data['signature']
def test_cloud_portal_dashboard_page_renders(tmp_path, monkeypatch):
 monkeypatch.setenv('SNAPKEY_PORTAL_DB', str(tmp_path/'portal.db'))
 import importlib
 import cloud_portal.api as api
 importlib.reload(api)
 client=TestClient(api.app)
 response=client.get('/portal')
 assert response.status_code==200
 assert 'SnapKey Eye' in response.text
 assert 'System Status' in response.text
 assert '/portal/cameras.html' in response.text


def test_cloud_camera_registry_is_scoped_and_idempotent(tmp_path, monkeypatch):
 monkeypatch.setenv('SNAPKEY_PORTAL_DB', str(tmp_path/'portal-camera.db'))
 monkeypatch.delenv('SNAPKEY_DATABASE_URL', raising=False)
 monkeypatch.setenv('SNAPKEY_ENV', 'development')
 import importlib
 import cloud_portal.api as api
 importlib.reload(api)
 client=TestClient(api.app)
 camera={'tenant_id':'tenant-a','company_code':'COMP1','shop_id':'SHOP1','site_id':'site-1','edge_id':'edge-1','camera_id':'CAM-001','name':'Main Entrance','source_type':'rtsp','source':'rtsp://camera.local/stream','camera_role':'ENTRANCE_EXIT','camera_zone':'Main Gate','crowd_threshold':10,'enabled':True,'features':{'face_recognition':True},'settings':{'tracking_fps':3}}
 response=client.put('/portal/v1/tenants/tenant-a/cameras/CAM-001',json=camera)
 assert response.status_code==200
 assert response.json()['camera']['shop_id']=='SHOP1'
 camera['name']='Main Gate Updated'
 assert client.put('/portal/v1/tenants/tenant-a/cameras/CAM-001',json=camera).status_code==200
 items=client.get('/portal/v1/tenants/tenant-a/cameras',params={'shop_id':'SHOP1','edge_id':'edge-1'}).json()['items']
 assert len(items)==1 and items[0]['name']=='Main Gate Updated'
 assert client.get('/portal/v1/tenants/tenant-b/cameras').json()['items']==[]
 assert client.delete('/portal/v1/tenants/tenant-a/cameras/CAM-001',params={'shop_id':'SHOP1','edge_id':'edge-1'}).status_code==200
 assert client.get('/portal/v1/tenants/tenant-a/cameras').json()['items']==[]


def test_cloud_camera_registry_rejects_path_scope_mismatch(tmp_path, monkeypatch):
 monkeypatch.setenv('SNAPKEY_PORTAL_DB', str(tmp_path/'portal-camera-scope.db'))
 monkeypatch.delenv('SNAPKEY_DATABASE_URL', raising=False)
 monkeypatch.setenv('SNAPKEY_ENV', 'development')
 import importlib
 import cloud_portal.api as api
 importlib.reload(api)
 client=TestClient(api.app)
 camera={'tenant_id':'tenant-b','shop_id':'SHOP1','site_id':'site-1','edge_id':'edge-1','camera_id':'CAM-001','name':'Main Entrance','source':'rtsp://camera.local/stream'}
 assert client.put('/portal/v1/tenants/tenant-a/cameras/CAM-001',json=camera).status_code==400
