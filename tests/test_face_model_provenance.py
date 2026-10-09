"""Synthetic templates/images and isolated databases; no real biometric/CRM data."""
import json
import sqlite3

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from camera_service import face_service
from camera_service.face_provenance import template_diagnostics
from camera_service.models import PersonnelCreate,PersonnelRole
from camera_service.storage import SQLiteStore

VECTOR=[1.]+[0.]*511


def person(store):
    return store.create_person(PersonnelCreate(employee_code='SYN-001',full_name='Synthetic',role=PersonnelRole.WORKER))


def test_local_enrollment_api_persists_verified_model_and_preview(tmp_path,monkeypatch):
    import camera_service.api as api
    store=SQLiteStore(str(tmp_path/'edge.db'));pid=person(store)['id']
    monkeypatch.setattr(api,'store',store)
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda *args:'verified-model')
    service=face_service.FaceService.__new__(face_service.FaceService)
    service._app=object();service._model_key='verified-model';service._det_size=(640,640)
    service.detect=lambda _: [{'bbox':(0,0,200,200),'embedding':np.asarray(VECTOR)}]
    monkeypatch.setattr(api,'get_face_service',lambda:service)
    preview=tmp_path/'preview.jpg'
    monkeypatch.setattr(api,'_save_face_preview',lambda _,image: (cv2.imwrite(str(preview),image) and str(preview)))
    ok,image=cv2.imencode('.jpg',np.zeros((200,200,3),np.uint8));assert ok
    client=TestClient(api.app)
    response=client.post(f'/api/v1/personnel/{pid}/faces',files={'file':('synthetic.jpg',image.tobytes(),'image/jpeg')})
    assert response.status_code==200,response.text
    face=response.json();assert face['model_key']=='verified-model'
    assert store.list_faces(pid)[0]['model_key']=='verified-model'
    assert len(store.embeddings())==1
    listed=client.get('/api/v1/personnel').json()['items'][0]
    assert listed['sync_diagnostic']['model_compatibility']=='COMPATIBLE'
    assert listed['primary_face_url']==face['image_url']
    assert client.get(face['image_url']).status_code==200
    assert 'embedding' not in json.dumps(listed) and 'base64' not in json.dumps(listed).lower()


def test_mixed_legacy_and_incompatible_templates_are_not_candidates(tmp_path,monkeypatch):
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda:'active')
    store=SQLiteStore(str(tmp_path/'edge.db'));pid=person(store)['id']
    store.add_face(pid,VECTOR,.9,model_key='active')
    store.add_face(pid,VECTOR,.9,model_key='other')
    store.add_face(pid,VECTOR,.9)
    diagnostic=template_diagnostics(store.list_faces(pid),'active')
    assert diagnostic['model_compatibility']=='UNVERIFIED'
    assert diagnostic['template_status_counts']==dict(UNVERIFIED=1,MODEL_UNAVAILABLE=0,INCOMPATIBLE=1,COMPATIBLE=1)
    assert len(diagnostic['templates'])==3 and len(store.embeddings())==1
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda: (_ for _ in ()).throw(ValueError('missing')))
    assert store.embeddings()==[]
    diagnostic=template_diagnostics(store.list_faces(pid),None)
    assert diagnostic['template_status_counts']['MODEL_UNAVAILABLE']==2
    assert diagnostic['template_status_counts']['UNVERIFIED']==1


def test_additive_migration_preserves_legacy_bytes_and_history(tmp_path,monkeypatch):
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda:'active')
    path=tmp_path/'legacy.db';raw=json.dumps(VECTOR)
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE face_profiles(id TEXT PRIMARY KEY,person_id TEXT NOT NULL,embedding_json TEXT NOT NULL,quality REAL NOT NULL,created_at TEXT NOT NULL,image_path TEXT)')
        conn.execute('INSERT INTO face_profiles VALUES(?,?,?,?,?,?)',('legacy','person',raw,.9,'2026-10-09','preserved.jpg'))
        conn.execute('CREATE TABLE edge_event_queue(id TEXT PRIMARY KEY,event_type TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL DEFAULT \'PENDING\',attempts INTEGER NOT NULL DEFAULT 0,last_error TEXT,created_at TEXT NOT NULL)')
        conn.execute('INSERT INTO edge_event_queue(id,event_type,payload_json,created_at) VALUES(?,?,?,?)',('history','PERSON_RECOGNIZED','{"person_id":"person","metadata":{}}','2026-10-09'))
    store=SQLiteStore(str(path))
    with store._conn() as conn:
        row=dict(conn.execute('SELECT * FROM face_profiles').fetchone())
        history=dict(conn.execute("SELECT * FROM edge_event_queue WHERE id='history'").fetchone())
    assert history['payload_json']=='{"person_id":"person","metadata":{}}' and history['status']=='PENDING' and history['attempts']==0
    assert row['embedding_json']==raw and row['image_path']=='preserved.jpg' and row['model_key'] is None
    assert template_diagnostics(store.list_faces('person'),'active')['model_compatibility']=='UNVERIFIED'
    assert store.embeddings()==[]
    SQLiteStore(str(path))
    assert store.list_faces('person')[0]=={k:v for k,v in row.items() if k!='embedding_json'}


@pytest.mark.parametrize('vector',[[0.]*512,[1.]*3,[float('nan')]*512,[float('inf')]*512])
def test_invalid_verified_embeddings_are_rejected(tmp_path,vector,monkeypatch):
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda:'active')
    store=SQLiteStore(str(tmp_path/'edge.db'));pid=person(store)['id']
    with pytest.raises(ValueError):store.add_face(pid,vector,.9,model_key='active')
    face=store.add_face(pid,VECTOR,.9,model_key='active')
    with store._conn() as conn:
        conn.execute('UPDATE face_profiles SET embedding_json=? WHERE id=?',(json.dumps(vector),face['id']))
    assert store.embeddings()==[]


def test_person_level_crm_claim_cannot_verify_legacy_template(tmp_path,monkeypatch):
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda:'active')
    store=SQLiteStore(str(tmp_path/'edge.db'));store.configure_event_scope('tenant',None,'shop','edge')
    item=dict(person_id='crm',employee_code='CRM',full_name='Synthetic',role='WORKER',crm_user_id='user',
        tenant_id='tenant',shop_id='shop',enrollment_model_key='active',faces=[dict(face_id='face',embedding=VECTOR,quality=.9)])
    store.apply_cloud_personnel([item])
    assert store.list_faces('crm')[0]['model_key'] is None and store.embeddings()==[]
    item['faces'][0]['model_key']='active'
    with pytest.raises(ValueError,match='new enrollment ID'):store.apply_cloud_personnel([item])
    item['faces'][0]['face_id']='verified';store.apply_cloud_personnel([item])
    assert len(store.embeddings())==1
    for changes in ({'tenant_id':'other'},{'shop_id':'other'}):
        with pytest.raises(ValueError):store.apply_cloud_personnel([{**item,**changes}])
    item['faces'][0]['model_key']='other'
    with pytest.raises(ValueError):store.apply_cloud_personnel([item])
    assert store.get_face('crm','verified')['model_key']=='active'
    assert store.get_face('crm','face')['model_key'] is None


def test_fingerprint_cache_invalidates_weights_configuration_and_missing_files(tmp_path,monkeypatch):
    paths=(tmp_path/'det.onnx',tmp_path/'rec.onnx')
    for path in paths:path.write_bytes(b'synthetic-weights')
    monkeypatch.setattr(face_service,'_model_files',lambda:paths)
    face_service.enrollment_model_key.cache_clear()
    first=face_service.enrollment_model_key()
    assert face_service.enrollment_model_key()==first
    assert face_service.enrollment_model_key.cache_info().hits==1
    assert face_service.enrollment_model_key((320,320))!=first
    service=face_service.FaceService.__new__(face_service.FaceService)
    service._app=object();service._model_key=first;service._det_size=(640,640)
    paths[1].write_bytes(b'different-synthetic-weights')
    assert face_service.enrollment_model_key()!=first
    with pytest.raises(ValueError,match='restart'):service.model_provenance()
    paths[1].unlink()
    with pytest.raises(ValueError,match='unavailable'):face_service.enrollment_model_key()
    face_service.enrollment_model_key.cache_clear()


def test_fallback_cannot_enroll_or_claim_provenance():
    service=face_service.FaceService.__new__(face_service.FaceService)
    service._app=None;service._model_key=None
    service.detect=lambda _: [dict(bbox=(0,0,200,200),embedding=None)]
    with pytest.raises(ValueError,match='embedding unavailable'):service.enroll(np.zeros((200,200,3),np.uint8))
    with pytest.raises(ValueError,match='provenance unavailable'):service.enroll_with_provenance(np.zeros((200,200,3),np.uint8))


def test_enrollment_rejects_model_change_during_inference(monkeypatch):
    active=['loaded']
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda *args:active[0])
    service=face_service.FaceService.__new__(face_service.FaceService)
    service._app=object();service._model_key='loaded';service._det_size=(640,640)
    def detect(_):
        active[0]='replaced'
        return [dict(bbox=(0,0,200,200),embedding=np.asarray(VECTOR))]
    service.detect=detect
    with pytest.raises(ValueError,match='restart'):service.enroll_with_provenance(np.zeros((200,200,3),np.uint8))


def test_edge_heartbeat_and_single_template_diagnostics(monkeypatch):
    from camera_service.sync_worker import EdgeSyncWorker
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda:'active')
    faces=[dict(id='compatible',model_key='active'),dict(id='legacy',model_key=None)]
    report=EdgeSyncWorker._personnel_model_status(faces)
    assert report['model_compatibility']=='UNVERIFIED'
    assert report['template_status_counts']['COMPATIBLE']==1
    assert report['template_status_counts']['UNVERIFIED']==1
    assert template_diagnostics([dict(id='different',model_key='other')],'active')['model_compatibility']=='INCOMPATIBLE'
    assert template_diagnostics([dict(id='missing',model_key='active')],None)['model_compatibility']=='MODEL_UNAVAILABLE'


def test_trusted_import_cannot_overwrite_existing_embedding_or_provenance(tmp_path,monkeypatch):
    monkeypatch.setattr(face_service,'enrollment_model_key',lambda:'active')
    store=SQLiteStore(str(tmp_path/'edge.db'))
    item=dict(person_id='crm',employee_code='CRM',full_name='Synthetic',role='WORKER',crm_user_id='user',
        tenant_id='tenant',shop_id='shop',faces=[dict(face_id='immutable',embedding=VECTOR,quality=.9,model_key='active')])
    store.apply_cloud_personnel([item]);store.apply_cloud_personnel([item])
    item['faces'][0]['embedding']=[0.,1.]+[0.]*510
    with pytest.raises(ValueError,match='embedding conflict'):store.apply_cloud_personnel([item])
    assert store.embeddings()[0]['embedding']==VECTOR
    assert store.get_face('crm','immutable')['model_key']=='active'
