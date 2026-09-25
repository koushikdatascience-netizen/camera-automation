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
 assert 'SnapKey Vision AI' in response.text and 'Cloud command center' in response.text
