"""Synthetic biometrics, isolated SQLite and mocked cloud requests only."""
import json
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from camera_service.storage import SQLiteStore


def item(**changes):
    return dict(person_id='crm-person',employee_code='employee',full_name='Synthetic',role='WORKER',
        crm_user_id='crm-user',tenant_id='tenant',shop_id='shop',faces=[],**changes)


def test_edge_duplicate_identity_and_scope_are_atomic(tmp_path):
    store=SQLiteStore(str(tmp_path/'edge.db'));store.configure_event_scope('tenant',None,'shop','edge')
    original=item();store.apply_cloud_personnel([original])
    for changes in ({'person_id':'other','employee_code':'other'}, {'tenant_id':'other'}, {'crm_user_id':'other'}):
        with pytest.raises(ValueError):store.apply_cloud_personnel([{**original,**changes}])
        assert len(store.list_people())==1
        assert json.loads(store.list_people()[0]['crm_sync_json'])['crm_user_id']=='crm-user'


def test_edge_model_and_embedding_validation(tmp_path,monkeypatch):
    from camera_service import face_service
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda:'model')
    store=SQLiteStore(str(tmp_path/'edge.db'))
    vector=[1.]+[0.]*511
    valid=item(enrollment_model_key='model');valid['faces']=[{'face_id':'face','embedding':vector,'quality':.9}]
    store.apply_cloud_personnel([valid]);assert len(store.embeddings())==1
    for invalid in ({**valid,'enrollment_model_key':'other'}, {**valid,'faces':[{'face_id':'bad','embedding':[0.]*512}]}):
        with pytest.raises(ValueError):store.apply_cloud_personnel([invalid])
        assert store.list_faces('crm-person')[0]['id']=='face'
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda:'changed-local-model')
    assert store.embeddings()==[] and len(store.list_faces('crm-person'))==1


def test_identity_collision_preserves_local_history(tmp_path):
    from camera_service.models import PersonnelCreate,PersonnelRole
    store=SQLiteStore(str(tmp_path/'edge.db'))
    person=store.create_person(PersonnelCreate(employee_code='employee',full_name='Local',role=PersonnelRole.WORKER))
    store.add_face(person['id'],[1.]+[0.]*511,.9)
    with pytest.raises(ValueError):store.apply_cloud_personnel([item()])
    assert store.get_person(person['id'])['active'] and len(store.list_faces(person['id']))==1


def test_local_preview_capability_tenant_scope_and_list_privacy(tmp_path,monkeypatch):
    import camera_service.api as api
    store=SQLiteStore(str(tmp_path/'edge.db'));store.apply_cloud_personnel([item()])
    monkeypatch.setattr(api,'store',store)
    monkeypatch.setattr(api,'config',SimpleNamespace(edge=SimpleNamespace(tenant_id='tenant',shop_id='shop',site_id='site')))
    calls=[]
    monkeypatch.setattr(api.cloud_client,'enrollment_preview',lambda pid:calls.append(pid) or b'\xff\xd8synthetic')
    client=TestClient(api.app)
    url='/api/v1/personnel/crm-person/enrollment-image'
    assert client.get(url).status_code==401 and calls==[]
    client.cookies.set('camera_eye_preview',api._preview_capability)
    assert client.get(url).status_code==200 and calls==['crm-person']
    data=client.get('/api/v1/personnel').json()['items'][0]
    assert data['primary_face_url']==url and 'crm_sync_json' not in data
    assert 'embedding' not in json.dumps(data) and 'base64' not in json.dumps(data).lower()
    api.config.edge.shop_id='other'
    assert client.get(url).status_code==403 and calls==['crm-person']
    assert client.get(url,headers={'Origin':'https://other.example'}).status_code==403


def test_edge_preview_never_follows_redirect_or_exposes_token(monkeypatch):
    from camera_service.cloud_client import CloudSyncClient
    import camera_service.cloud_client as module
    captured={}
    response=SimpleNamespace(status_code=302,headers={},raise_for_status=lambda:None,close=lambda:None)
    def get(url,**kwargs):captured.update(kwargs);return response
    monkeypatch.setattr(module.requests,'get',get)
    client=CloudSyncClient(SimpleNamespace(enabled=True,base_url='https://cloud.example',api_token='synthetic',timeout_seconds=1))
    with pytest.raises(ValueError):client.enrollment_preview('person')
    assert captured['allow_redirects'] is False
