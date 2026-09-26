from fastapi.testclient import TestClient


def portal_auth(client, monkeypatch, tenant='tenant-a', shop='SHOP1'):
 monkeypatch.setenv('SNAPKEY_CRM_INTEGRATION_KEY','test-crm-integration-key-0123456789')
 response=client.post('/crm/session',json={'tenantId':tenant,'companyCode':'COMP1','shopCode':shop,'userId':'user-1','displayName':'Test User','role':'OWNER'},headers={'X-CRM-Integration-Key':'test-crm-integration-key-0123456789'})
 assert response.status_code==200
 from urllib.parse import urlparse, parse_qs
 token=parse_qs(urlparse(response.json()['launchUrl']).fragment)['session'][0]
 return {'Authorization':'Bearer '+token}


def test_cloud_portal_ingests_events_by_tenant(tmp_path, monkeypatch):
 monkeypatch.setenv('SNAPKEY_PORTAL_DB', str(tmp_path/'portal.db'))
 monkeypatch.setenv('SNAPKEY_EDGE_API_TOKEN', 'test-edge-token')
 monkeypatch.setenv('SNAPKEY_ENV', 'development')
 import importlib
 import cloud_portal.api as api
 importlib.reload(api)
 client=TestClient(api.app)
 auth=portal_auth(client,monkeypatch)
 envelope={'schema_version':'edge.event.v1','tenant_id':'tenant-a','site_id':'site-1','edge_id':'edge-1','event_id':'evt-1','event_type':'SECURITY_OBJECT_ALERT','event_time':'2026-08-28T09:00:00+00:00','store_id':'store-a','camera_id':'cam-1','payload':{'event_id':'evt-1','metadata':{'object_label':'scissors'}}}
 assert client.post('/edge/v1/events',json=envelope,headers={'Authorization':'Bearer test-edge-token'}).status_code==200
 assert client.get('/portal/v1/tenants/tenant-a/summary',headers=auth).json()['events']['SECURITY_OBJECT_ALERT']==1
 items=client.get('/portal/v1/tenants/tenant-a/events',params={'site_id':'site-1'},headers=auth).json()['items']
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
 auth=portal_auth(client,monkeypatch)
 camera={'tenant_id':'tenant-a','company_code':'COMP1','shop_id':'SHOP1','site_id':'site-1','edge_id':'edge-1','camera_id':'CAM-001','name':'Main Entrance','source_type':'rtsp','source':'rtsp://camera.local/stream','camera_role':'ENTRANCE_EXIT','camera_zone':'Main Gate','crowd_threshold':10,'enabled':True,'features':{'face_recognition':True},'settings':{'tracking_fps':3}}
 response=client.put('/portal/v1/tenants/tenant-a/cameras/CAM-001',json=camera,headers=auth)
 assert response.status_code==200
 assert response.json()['camera']['shop_id']=='SHOP1'
 camera['name']='Main Gate Updated'
 assert client.put('/portal/v1/tenants/tenant-a/cameras/CAM-001',json=camera,headers=auth).status_code==200
 items=client.get('/portal/v1/tenants/tenant-a/cameras',params={'shop_id':'SHOP1','edge_id':'edge-1'},headers=auth).json()['items']
 assert len(items)==1 and items[0]['name']=='Main Gate Updated'
 assert client.get('/portal/v1/tenants/tenant-b/cameras',headers=auth).status_code==403
 assert client.delete('/portal/v1/tenants/tenant-a/cameras/CAM-001',params={'shop_id':'SHOP1','edge_id':'edge-1'},headers=auth).status_code==200
 assert client.get('/portal/v1/tenants/tenant-a/cameras',headers=auth).json()['items']==[]


def test_cloud_camera_registry_rejects_path_scope_mismatch(tmp_path, monkeypatch):
 monkeypatch.setenv('SNAPKEY_PORTAL_DB', str(tmp_path/'portal-camera-scope.db'))
 monkeypatch.delenv('SNAPKEY_DATABASE_URL', raising=False)
 monkeypatch.setenv('SNAPKEY_ENV', 'development')
 import importlib
 import cloud_portal.api as api
 importlib.reload(api)
 client=TestClient(api.app)
 auth=portal_auth(client,monkeypatch)
 camera={'tenant_id':'tenant-b','shop_id':'SHOP1','site_id':'site-1','edge_id':'edge-1','camera_id':'CAM-001','name':'Main Entrance','source':'rtsp://camera.local/stream'}
 assert client.put('/portal/v1/tenants/tenant-a/cameras/CAM-001',json=camera,headers=auth).status_code==400


def test_portal_edge_command_roundtrip_is_scope_bound(tmp_path, monkeypatch):
 monkeypatch.setenv('SNAPKEY_PORTAL_DB', str(tmp_path/'commands.db'))
 monkeypatch.delenv('SNAPKEY_DATABASE_URL', raising=False)
 monkeypatch.setenv('SNAPKEY_ENV','development')
 import importlib
 import cloud_portal.api as api
 importlib.reload(api)
 command=api.store.create_edge_command({'tenant_id':'tenant-a','shop_id':'SHOP1','edge_id':'edge-1','command_type':'CAMERA_TEST','request':{'source':'rtsp://camera/live'}})
 claimed=api.store.claim_edge_commands('tenant-a','SHOP1','edge-1')
 assert len(claimed)==1 and claimed[0]['id']==command['id']
 assert api.store.claim_edge_commands('tenant-a','SHOP2','edge-1')==[]
 assert api.store.complete_edge_command(command['id'],'tenant-a','SHOP1','edge-1','SUCCEEDED',{'ok':True,'connected':True})
 result=api.store.get_edge_command(command['id'],'tenant-a')
 assert result['status']=='SUCCEEDED' and result['result']['connected'] is True
 assert api.store.get_edge_command(command['id'],'tenant-b') is None


def test_crm_portal_session_is_hashed_and_scope_bound(tmp_path, monkeypatch):
 monkeypatch.setenv('SNAPKEY_PORTAL_DB',str(tmp_path/'crm-session.db'))
 monkeypatch.setenv('SNAPKEY_CRM_INTEGRATION_KEY','crm-secret-key-012345678901234567890123')
 monkeypatch.setenv('SNAPKEY_PUBLIC_BASE_URL','https://camera.example.test')
 monkeypatch.delenv('SNAPKEY_DATABASE_URL',raising=False)
 import importlib
 import cloud_portal.api as api
 importlib.reload(api)
 client=TestClient(api.app)
 response=client.post('/crm/session',json={'tenantId':'tenant-a','companyCode':'2','shopCode':'SHOP1','userId':'u1','displayName':'Owner','role':'OWNER'},headers={'X-CRM-Integration-Key':'crm-secret-key-012345678901234567890123'})
 assert response.status_code==200
 from urllib.parse import urlparse,parse_qs
 token=parse_qs(urlparse(response.json()['launchUrl']).fragment)['session'][0]
 status=client.get('/session/status',headers={'Authorization':'Bearer '+token})
 assert status.status_code==200 and status.json()['shopCode']=='SHOP1'
 assert token not in str(api.store.portal_session_by_hash(api._token_digest(token)))
 assert client.get('/portal/v1/tenants/tenant-b/cameras',headers={'Authorization':'Bearer '+token}).status_code==403
