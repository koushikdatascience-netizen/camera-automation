"""Isolated SQLite and mock inference/CRM; no remote mutations or model downloads."""
import base64
import threading
import time
from types import SimpleNamespace
import cv2
import numpy as np
import pytest
from fastapi import HTTPException
from cloud_portal import api
from cloud_portal.storage import PortalStore
from cloud_portal.enrollment_worker import EnrollmentWorker
from camera_service.face_service import FaceService, validated_embedding


def image(value=0):
    ok,data=cv2.imencode('.jpg',np.full((32,32,3),value,dtype=np.uint8))
    assert ok
    return base64.b64encode(data).decode()


@pytest.fixture
def mirror(tmp_path,monkeypatch):
    store=PortalStore(str(tmp_path/'portal.db'))
    state=SimpleNamespace(calls=0,users=[{'id':'user-1','tenantId':'uuid-1','tenantCode':'tenant-1',
        'name':'Test Employee','isActive':True,'faceImages':[image()],'faceEmbeddings':None}])
    def enroll(_):
        state.calls+=1
        return [1.]+[0.]*511,.9
    monkeypatch.setattr(api,'store',store)
    monkeypatch.setattr(api,'crm_client',SimpleNamespace(face_attendance_configured=True,face_embeddings=lambda _, **kwargs:state.users))
    monkeypatch.setattr(api,'_cloud_face_enroller',lambda:SimpleNamespace(enroll=enroll))
    monkeypatch.setattr(api,'enrollment_model_key',lambda:'model-v1')
    monkeypatch.setattr(api,'_crm_personnel_last_refresh',{})
    return state,store


def refresh(): api._refresh_crm_personnel_locked('tenant-1','shop-1')
def person_id(store): return store.list_crm_person_mappings('tenant-1','shop-1')[0]['local_person_id']
def faces(store): return store.list_cloud_faces('tenant-1','shop-1',person_id(store),include_embedding=True)


def test_employee_code_change_preserves_identity_and_templates(mirror):
    state,store=mirror; refresh(); pid=person_id(store); original=faces(store)[0]['id']
    state.users[0]['employeeCode']='changed-code'
    refresh()
    assert person_id(store)==pid and faces(store)[0]['id']==original
    assert len(store.list_cloud_people('tenant-1','shop-1'))==1


def test_duplicate_crm_user_rejected_before_mutation(mirror):
    state,store=mirror; state.users.append(dict(state.users[0],employeeCode='other'))
    with pytest.raises(HTTPException,match=''): refresh()
    assert store.list_cloud_people('tenant-1','shop-1')==[]


def test_mapping_cannot_duplicate_or_reassign_identity(mirror):
    _,store=mirror;refresh();pid=person_id(store)
    with pytest.raises(ValueError):
        store.upsert_crm_person_mapping({'tenant_id':'tenant-1','shop_id':'shop-1','local_person_id':'other','crm_user_id':'user-1'})
    with pytest.raises(ValueError):
        store.upsert_crm_person_mapping({'tenant_id':'tenant-1','shop_id':'shop-1','local_person_id':pid,'crm_user_id':'other'})


def test_refresh_preserves_existing_break_mapping(mirror):
    _,store=mirror;refresh();pid=person_id(store)
    mapping=store.crm_person_mapping('tenant-1','shop-1',pid)
    store.upsert_crm_person_mapping({**mapping,'break_master_id':'approved-break'})
    refresh()
    assert store.crm_person_mapping('tenant-1','shop-1',pid)['break_master_id']=='approved-break'


def portal_principal(tenant='tenant-1',shop='shop-1'):
    return api.PortalPrincipal('session',tenant,None,shop,'admin','Admin','ADMIN')


def test_authenticated_preview_and_scope(mirror):
    _,store=mirror;refresh();pid=person_id(store)
    response=api.portal_enrollment_preview('tenant-1',pid,portal_principal())
    assert response.media_type=='image/jpeg' and response.body.startswith(b'\xff\xd8')
    assert response.headers['cache-control']=='no-store'
    with pytest.raises(HTTPException) as exc:
        api.portal_enrollment_preview('tenant-1',pid,portal_principal('other'))
    assert exc.value.status_code==403
    with pytest.raises(HTTPException) as exc:
        api.portal_enrollment_preview('tenant-1',pid,portal_principal(shop='other'))
    assert exc.value.status_code==404


def test_preview_route_requires_authentication(mirror):
    from fastapi.testclient import TestClient
    _,store=mirror;refresh();pid=person_id(store)
    client=TestClient(api.app)
    assert client.get('/portal/v1/tenants/tenant-1/personnel/'+pid+'/enrollment-image').status_code==401
    assert client.get('/edge/v1/personnel/'+pid+'/enrollment-image').status_code==401


@pytest.mark.parametrize('field',['faceImages','profileImage'])
def test_preview_supports_both_image_fields(mirror,field):
    state,store=mirror;state.users[0].pop('faceImages');state.users[0][field]=image()
    refresh();assert api._enrollment_preview('tenant-1','shop-1',person_id(store)).body


def test_missing_preview_is_explicit(mirror):
    state,store=mirror;state.users[0]['faceImages']=[];refresh()
    assert not faces(store)
    with pytest.raises(HTTPException):api._enrollment_preview('tenant-1','shop-1',person_id(store))


def test_available_image_preview_does_not_require_successful_enrollment(mirror,monkeypatch):
    _,store=mirror
    monkeypatch.setattr(api,'_cloud_face_enroller',lambda:SimpleNamespace(enroll=lambda _:(_ for _ in ()).throw(ValueError('ambiguous face'))))
    refresh();assert faces(store)==[]
    monkeypatch.setattr(api,'_refresh_crm_personnel',lambda *a,**kw:None)
    result=api.portal_personnel('tenant-1',portal_principal())['items'][0]
    assert result['enrollment_preview_url'] and not result['face_enrolled']
    assert api._enrollment_preview('tenant-1','shop-1',person_id(store)).body


def test_diagnostic_is_read_only_and_contains_no_biometrics(mirror,monkeypatch):
    _,store=mirror;refresh()
    monkeypatch.setattr(api.crm_client,'face_embeddings',lambda *a,**k:pytest.fail('Read-only diagnostic fetched CRM'))
    result=api.personnel_diagnostics('tenant-1',portal_principal())
    import json
    assert result['items'][0]['cloud_face_count']==1
    assert result['items'][0]['model_compatibility']=='COMPATIBLE'
    assert result['items'][0]['template_status_counts']['COMPATIBLE']==1
    assert result['items'][0]['templates'][0]['enrollment_model_key']=='model-v1'
    assert 'embedding' not in json.dumps(result) and 'base64' not in json.dumps(result).lower()


def test_face_login_duplicate_and_tenant_mismatch_rejected(mirror):
    state,_=mirror
    state.users.append(dict(state.users[0]))
    with pytest.raises(RuntimeError):api._crm_face_login_identity('tenant-1','user-1')
    state.users=state.users[:1];state.users[0]['tenantCode']='other'
    with pytest.raises(RuntimeError):api._crm_face_login_identity('tenant-1','user-1')


def test_face_login_returned_identity_and_tenant_checked():
    for user in ({'id':'other','tenantId':'uuid-1'},{'id':'user-1','tenantId':'other'}):
        with pytest.raises(RuntimeError):
            api._validated_crm_face_login_token({'success':True,'user':user,'token':'synthetic'},'user-1','uuid-1')


def test_persistent_image_cache_restart_and_change(mirror,monkeypatch):
    state,store=mirror
    refresh(); assert state.calls==1 and len(faces(store))==1
    original=faces(store)[0]['id']
    assert faces(store)[0]['model_key']=='model-v1'
    refresh(); assert state.calls==1
    monkeypatch.setattr(api,'store',PortalStore(store.path))
    refresh(); assert state.calls==1
    state.users[0]['faceImages']=[image(90)]
    refresh(); assert state.calls==2 and len(faces(store))==1
    assert faces(store)[0]['id']!=original
    monkeypatch.setattr(api,'enrollment_model_key',lambda:'model-v2')
    refresh(); assert state.calls==3
    assert faces(store)[0]['model_key']=='model-v2'


def test_unverified_cached_model_not_exported_until_refresh(mirror,monkeypatch):
    state,store=mirror;refresh();pid=person_id(store);face=faces(store)[0]
    store.set_cloud_face_model_key('tenant-1','shop-1',pid,face['id'],None)
    monkeypatch.setattr(api,'_refresh_crm_personnel',lambda *a,**kw:None)
    principal=api.EdgePrincipal(tenant_id='tenant-1',company_code=None,shop_id='shop-1',site_id='site',edge_id='edge')
    assert api.edge_personnel_config(principal)['items'][0]['faces']==[]
    refresh()
    assert api.edge_personnel_config(principal)['items'][0]['enrollment_model_key']=='model-v1'
    assert state.calls==2  # Actual regeneration, never an availability-only backfill.
    legacy=next(f for f in faces(store) if f['id']==face['id'])
    assert legacy['model_key'] is None and legacy['embedding']==face['embedding']
    exported=api.edge_personnel_config(principal)['items'][0]['faces']
    assert len(exported)==1 and exported[0]['model_key']=='model-v1'
    refresh();assert state.calls==2


def test_multiple_images_revoke_and_preserve_local(mirror):
    state,store=mirror
    state.users[0]['faceImages'].append(image(120))
    store.create_cloud_person({'id':'local','tenant_id':'tenant-1','shop_id':'shop-1',
        'employee_code':'LOCAL','full_name':'Local','role':'WORKER'})
    refresh(); assert len(faces(store))==2
    state.users[0]['isActive']=False
    refresh(); assert faces(store)==[]
    state.users=[]
    refresh()
    assert store.get_cloud_person('tenant-1','shop-1','local')['active']
    assert not store.get_cloud_person('tenant-1','shop-1',person_id(store))['active']


@pytest.mark.parametrize('raw',[{}, {'data':None}, {'success':False,'data':[]}, {'success':'false','data':[]}, {'data':{}}, [None], [{'id':''}]])
def test_malformed_response_cannot_remove_templates(mirror,raw):
    state,store=mirror;refresh();state.users=raw
    with pytest.raises(HTTPException): refresh()
    assert len(faces(store))==1
    assert store.get_cloud_person('tenant-1','shop-1',person_id(store))['active']


def test_crm_outage_retains_templates(mirror,monkeypatch):
    _,store=mirror;refresh()
    def fail(_): raise RuntimeError('offline')
    monkeypatch.setattr(api.crm_client,'face_embeddings',fail)
    with pytest.raises(RuntimeError):refresh()
    assert len(faces(store))==1


def test_unknown_crm_model_vectors_never_accepted(mirror):
    state,store=mirror
    state.users[0]['faceImages']=[]
    state.users[0]['faceEmbeddings']=[1.]*512
    refresh(); assert faces(store)==[] and state.calls==0


@pytest.mark.parametrize('field,value',[('tenantCode','other'),('tenantId',''),('shopId','other-shop')])
def test_scope_fail_closed(mirror,field,value):
    state,store=mirror;state.users[0][field]=value
    with pytest.raises(HTTPException):refresh()
    assert not store.list_cloud_people('tenant-1','shop-1')


def test_single_flight_bound_queue_and_shutdown():
    worker=EnrollmentWorker(concurrency=1,capacity=1)
    started=threading.Event();release=threading.Event();calls=[]
    def run():started.set();release.wait(2);calls.append(1)
    assert worker.submit('scope',run);assert started.wait(2)
    assert not worker.submit('scope',run)
    assert not worker.submit('other',run)
    release.set();worker.shutdown()
    assert calls==[1] and worker.status()['completed']==1
    assert not worker.submit('new',run)


def test_background_failure_backoff():
    worker=EnrollmentWorker(retry_seconds=10)
    def fail():raise RuntimeError('mock unavailable')
    assert worker.submit('scope',fail)
    deadline=time.monotonic()+2
    while worker.status()['queue_depth'] and time.monotonic()<deadline:time.sleep(.005)
    assert worker.status()['failed']==1
    assert not worker.submit('scope',fail)
    worker.shutdown()


@pytest.mark.parametrize('count',[0,2])
def test_enrollment_rejects_zero_multiple_faces(count):
    service=FaceService.__new__(FaceService)
    service.detect=lambda _: [{'bbox':(0,0,100,100),'embedding':np.ones(512)}]*count
    service.quality=lambda *_:.9
    with pytest.raises(ValueError):service.enroll(np.zeros((100,100,3),np.uint8))


@pytest.mark.parametrize('vector',[[0.]*512,[float('nan')]*512,[float('inf')]*512,[1.]*128])
def test_invalid_embedding_rejected(vector):
    with pytest.raises(ValueError):validated_embedding(vector)


def test_normalization_and_ambiguous_matching():
    vector=validated_embedding([2.]*512)
    assert np.linalg.norm(vector)==pytest.approx(1)
    service=FaceService.__new__(FaceService)
    service.store=SimpleNamespace(embeddings=lambda:[{'person_id':'a','embedding':vector},
        {'person_id':'b','embedding':vector},{'person_id':'c','embedding':[0.]*512}])
    with pytest.raises(ValueError,match="ambiguous"):
        service.recognize(vector,.5)


def test_async_refresh_returns_existing_without_inference(mirror,monkeypatch):
    _,store=mirror;refresh()
    submitted=[]
    monkeypatch.setattr(api,'_crm_enrollment_worker',SimpleNamespace(submit=lambda key,operation:submitted.append(key)))
    api._refresh_crm_personnel('tenant-1','shop-1')
    assert submitted==[('tenant-1','shop-1')]
    assert len(faces(store))==1


def test_multi_camera_inference_pressure_is_bounded():
    from concurrent.futures import ThreadPoolExecutor
    service=FaceService.__new__(FaceService)
    service._lock=threading.RLock();service._last_inference=0.
    started=threading.Event();release=threading.Event();calls=[]
    def get(_):started.set();release.wait(2);calls.append(1);return []
    service._app=SimpleNamespace(get=get)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first=executor.submit(service.detect,np.zeros((32,32,3),np.uint8))
        assert started.wait(2)
        with pytest.raises(RuntimeError,match='busy'):
            service.detect(np.zeros((32,32,3),np.uint8))
        release.set();assert first.result()==[]
    assert calls==[1]

def test_jpeg_raw_base64_is_not_a_path():
    source=image(23)
    assert source.startswith('/9j/')
    assert api._decode_crm_face_image(source).startswith(b'\xff\xd8\xff')
    assert api._decode_crm_face_image('/private/face.jpg') is None


def test_same_crm_user_in_two_shops_is_isolated(mirror):
    state,store=mirror;refresh()
    api._refresh_crm_personnel_locked('tenant-1','shop-2')
    first=store.list_crm_person_mappings('tenant-1','shop-1')[0]['local_person_id']
    second=store.list_crm_person_mappings('tenant-1','shop-2')[0]['local_person_id']
    assert first!=second
    assert len(store.list_cloud_faces('tenant-1','shop-2',second))==1


def test_store_upsert_cannot_steal_identity(mirror):
    _,store=mirror
    item={'id':'shared','tenant_id':'tenant-1','shop_id':'shop-1','employee_code':'A','full_name':'A','role':'WORKER'}
    store.upsert_crm_cloud_person(item)
    with pytest.raises(ValueError):store.upsert_crm_cloud_person({**item,'tenant_id':'other'})
    assert store.get_cloud_person('tenant-1','shop-1','shared')

def test_malformed_image_keeps_valid_templates(mirror):
    state,store=mirror;refresh();state.users[0]['faceImages']=['not valid base64']
    with pytest.raises(HTTPException):refresh()
    assert len(faces(store))==1


def test_omitted_image_field_does_not_revoke_templates(mirror):
    state,store=mirror;refresh();del state.users[0]['faceImages']
    refresh();assert len(faces(store))==1

def test_rejected_image_cached_across_restart(mirror,monkeypatch):
    state,store=mirror
    def invalid(_):state.calls+=1;raise ValueError('no usable face')
    monkeypatch.setattr(api,'_cloud_face_enroller',lambda:SimpleNamespace(enroll=invalid))
    refresh();refresh();assert state.calls==1 and faces(store)==[]
    monkeypatch.setattr(api,'store',PortalStore(store.path));refresh();assert state.calls==1
    state.users[0]['faceImages']=[image(88)];refresh();assert state.calls==2

def test_low_similarity_ties_remain_unknown_not_recognition_errors():
    service=FaceService.__new__(FaceService)
    enrolled=[1.]+[0.]*511
    service.store=SimpleNamespace(embeddings=lambda:[{'person_id':'a','embedding':enrolled},
        {'person_id':'b','embedding':enrolled}])
    query=[0.,1.]+[0.]*510
    match,score=service.recognize(query,.55)
    assert match is None and score==pytest.approx(0)
