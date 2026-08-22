from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import cv2, numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Query
from camera_service.config import load_config
from camera_service.models import PersonnelCreate, PersonnelPatch
from camera_service.storage import SQLiteStore
from camera_service.face_service import FaceService
from camera_service.attendance_engine import AttendanceEngine
from camera_service.camera.supervisor import CameraSupervisor
from camera_service.camera_manager import CameraManager, CameraConfig, CameraStatus, CameraState
from camera_service.alert_dispatcher import AlertDispatcher
from camera_service.cloud_client import CloudSyncClient
from camera_service.licensing import LicenseManager
from typing import Optional
from pydantic import BaseModel
import json
import os
import webbrowser
import threading
import time
import socket
import sys
from pathlib import Path

config=load_config(); store=SQLiteStore(config.database_path); face_service=FaceService(store); attendance_engine=AttendanceEngine(store,config.store_id); supervisor=CameraSupervisor(config,store,face_service,attendance_engine)
camera_manager=CameraManager(config.database_path)
cloud_client=CloudSyncClient(config.cloud_sync)
alert_dispatcher=AlertDispatcher(config.alerts)
license_manager=LicenseManager(config.edge)

@asynccontextmanager
async def lifespan(app:FastAPI):
    supervisor.start(); yield; supervisor.shutdown()
app=FastAPI(title='Camera Automation Production P0',lifespan=lifespan)

def get_store(): return store

def _face_image_url(person_id: str, face_id: str) -> str:
    return f"/api/v1/personnel/{person_id}/faces/{face_id}/image"

def _save_face_preview(person_id: str, image) -> str | None:
    try:
        output_dir = Path(config.evidence_dir).parent / "personnel_faces" / person_id
        output_dir.mkdir(parents=True, exist_ok=True)
        h, w = image.shape[:2]
        scale = min(240 / max(h, w), 1.0)
        if scale < 1.0:
            image = cv2.resize(image, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
        path = output_dir / f"{int(time.time() * 1000)}.jpg"
        if cv2.imwrite(str(path), image):
            return str(path)
    except Exception:
        return None
    return None

@app.get('/health')
def health():
    statuses = [camera_manager.get_camera_status(cam.camera_id) for cam in camera_manager.list_cameras()]
    return {
        'status':'ok',
        'store_id':config.store_id,
        'cameras':len(camera_manager.list_cameras()),
        'online_cameras':sum(1 for status in statuses if status and status.online),
    }

@app.get('/ready')
def ready(): return {'status':'ready','camera_supervisor_running':supervisor.is_running()}

@app.get('/api/v1/edge/status')
def edge_status():
    license_status = license_manager.status()
    return {
        'edge': config.edge.model_dump(),
        'license': license_status.model_dump(),
        'cloud_sync_enabled': cloud_client.enabled(),
        'cloud_sync_allowed': cloud_client.enabled() and license_status.active,
        'queue': store.event_queue_status(),
        'alert_recipients': alert_dispatcher.preview_recipients(),
        'evidence': config.evidence.model_dump(),
    }

@app.post('/api/v1/edge/sync')
def sync_edge_events():
    license_status = license_manager.status()
    if not cloud_client.enabled():
        return {'synced': 0, 'failed': 0, 'enabled': False, 'message': 'Cloud sync is disabled'}
    if not license_status.active:
        return {'synced': 0, 'failed': 0, 'enabled': True, 'blocked': True, 'license': license_status.model_dump()}

    synced = 0
    failed = 0
    for row in store.queued_events(config.cloud_sync.batch_size):
        event = json.loads(row['payload_json'])
        try:
            cloud_client.post_event(config.edge, event)
            store.mark_event_synced(row['id'])
            synced += 1
        except Exception as exc:
            store.mark_event_failed(row['id'], str(exc))
            failed += 1
    return {'synced': synced, 'failed': failed, 'enabled': True}

@app.get('/api/v1/license/status')
def license_status():
    return license_manager.status().model_dump()

@app.get('/api/v1/license/machine-code')
def license_machine_code():
    return {'machine_code': license_manager.machine_code()}

@app.get('/api/v1/alerts/preview')
def alert_preview():
    license_status = license_manager.status()
    if config.edge.activation_required and not license_status.active:
        return {
            'items': [],
            'delivery_allowed': False,
            'license': license_status.model_dump(),
            'message': 'Paid alert delivery requires activation from the platform.',
        }

    previews = []
    for row in store.queued_events(20):
        event = json.loads(row['payload_json'])
        if alert_dispatcher.should_alert(event.get('event_type')):
            previews.append({
                'event_id': row['id'],
                'event_type': event.get('event_type'),
                'message': alert_dispatcher.format_message(event),
                'recipients': alert_dispatcher.preview_recipients(),
            })
    return {'items': previews, 'delivery_allowed': True}

# Setup UI Route
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response, StreamingResponse
import os

# Mount static files. check_dir=False keeps fresh/source-only installs from
# failing at import time if no static assets are currently present.
app.mount(
    "/static",
    StaticFiles(
        directory=os.path.join(os.path.dirname(__file__), "web", "static"),
        check_dir=False,
    ),
    name="static",
)

@app.get("/setup", response_class=HTMLResponse)
async def setup_ui():
    try:
        with open(os.path.join(os.path.dirname(__file__), "web", "setup.html"), "r", encoding="utf-8") as f:
            return HTMLResponse(content=f.read(), status_code=200)
    except FileNotFoundError:
        return HTMLResponse(content="<h1>Setup UI not found</h1>", status_code=404)

# Camera CRUD APIs
class CameraCreate(BaseModel):
    camera_id: str
    name: str
    source_type: str = "rtsp"
    rtsp_url: str
    enabled: bool = True
    camera_role: str = "GENERAL"
    camera_zone: str = "inside"
    crowd_threshold: int = 10
    tracking_fps: float = 3.0
    tracking_imgsz: int = 384
    tracking_quality: int = 65
    tracking_mode: str = "detect"
    features: dict = {}

@app.post('/api/v1/cameras')
def create_camera(camera_data: CameraCreate):
    try:
        config = camera_manager.create_camera(camera_data.model_dump())
        return {
            'success': True,
            'camera': {
                **config.model_dump(),
                'rtsp_url': camera_manager._mask_rtsp_password(config.rtsp_url)
            }
        }
    except Exception as e:
        raise HTTPException(400, str(e))

@app.get('/api/v1/cameras')
def list_cameras():
    cameras = camera_manager.list_cameras()
    items = []
    for cam in cameras:
        status = camera_manager.get_camera_status(cam.camera_id)
        items.append({
            **cam.model_dump(),
            'rtsp_url': camera_manager._mask_rtsp_password(cam.rtsp_url),
            'status': status.model_dump() if status else None,
        })
    return {
        'items': items
    }

@app.get('/api/v1/cameras/{camera_id}')
def get_camera(camera_id: str, include_secret: bool = False):
    camera = camera_manager.get_camera(camera_id)
    if not camera:
        raise HTTPException(404, 'Camera not found')
    rtsp_url = camera.rtsp_url if include_secret else camera_manager._mask_rtsp_password(camera.rtsp_url)
    return {
        **camera.model_dump(),
        'rtsp_url': rtsp_url
    }

@app.patch('/api/v1/cameras/{camera_id}')
def update_camera(camera_id: str, updates: dict):
    camera = camera_manager.update_camera(camera_id, updates)
    if not camera:
        raise HTTPException(404, 'Camera not found')
    return {
        **camera.model_dump(),
        'rtsp_url': camera_manager._mask_rtsp_password(camera.rtsp_url)
    }

@app.delete('/api/v1/cameras/{camera_id}')
def delete_camera(camera_id: str):
    if not camera_manager.delete_camera(camera_id):
        raise HTTPException(404, 'Camera not found')
    camera_manager.delete_camera_status(camera_id)
    return {'deleted': True}

# RTSP Test Connection API
class RTSPTestRequest(BaseModel):
    rtsp_url: str

@app.post('/api/v1/cameras/test')
def test_rtsp_connection(request: RTSPTestRequest):
    result = camera_manager.test_rtsp_connection(request.rtsp_url)
    return result

@app.get('/api/v1/cameras/test/snapshot')
def test_rtsp_snapshot(url: str = Query(...)):
    snapshot = camera_manager.read_rtsp_snapshot(url)
    if not snapshot:
        raise HTTPException(404, 'No snapshot available')
    return Response(content=snapshot, media_type='image/jpeg')

# Camera Status API
@app.get('/api/v1/cameras/{camera_id}/status')
def get_camera_status(camera_id: str):
    status = camera_manager.get_camera_status(camera_id)
    if not status:
        raise HTTPException(404, 'Camera status not found')
    return status.model_dump()

# Camera Control APIs
@app.post('/api/v1/cameras/{camera_id}/start')
def start_camera(camera_id: str):
    camera = camera_manager.get_camera(camera_id)
    if not camera:
        raise HTTPException(404, 'Camera not found')
    result = camera_manager.test_rtsp_connection(camera.rtsp_url)
    state = CameraState.ONLINE if result.get('success') else CameraState.DEGRADED
    camera_manager.update_camera_status(camera_id, CameraStatus(
        camera_id=camera_id,
        name=camera.name,
        state=state,
        online=bool(result.get('success')),
        capture_fps=float(result.get('fps') or 0),
        frames_received=int(result.get('frames_received') or 0),
        last_error=None if result.get('success') else result.get('message', 'Unable to open stream'),
    ))
    return {'status': state.value.lower(), 'camera_id': camera_id, 'diagnostics': result}

@app.post('/api/v1/cameras/{camera_id}/stop')
def stop_camera(camera_id: str):
    camera = camera_manager.get_camera(camera_id)
    if not camera:
        raise HTTPException(404, 'Camera not found')
    camera_manager.update_camera_status(camera_id, CameraStatus(
        camera_id=camera_id,
        name=camera.name,
        state=CameraState.STOPPED,
        online=False,
    ))
    return {'status': 'stopping', 'camera_id': camera_id}

@app.post('/api/v1/cameras/{camera_id}/restart')
def restart_camera(camera_id: str):
    stop_camera(camera_id)
    started = start_camera(camera_id)
    started['status'] = 'restarted_' + started['status']
    return started

# Camera Snapshot API
@app.get('/api/v1/cameras/{camera_id}/snapshot')
def get_camera_snapshot(camera_id: str):
    snapshot = camera_manager.get_camera_snapshot(camera_id)
    if not snapshot:
        raise HTTPException(404, 'No snapshot available')
    return Response(content=snapshot, media_type='image/jpeg')

@app.get('/api/v1/cameras/{camera_id}/stream')
def stream_camera(camera_id: str):
    camera = camera_manager.get_camera(camera_id)
    if not camera:
        raise HTTPException(404, 'Camera not found')
    return StreamingResponse(
        camera_manager.iter_rtsp_mjpeg(camera.rtsp_url),
        media_type='multipart/x-mixed-replace; boundary=frame',
    )

@app.get('/api/v1/cameras/{camera_id}/tracking-stream')
def stream_camera_tracking(camera_id: str):
    camera = camera_manager.get_camera(camera_id)
    if not camera:
        raise HTTPException(404, 'Camera not found')
    return StreamingResponse(
        camera_manager.iter_tracking_mjpeg(camera, config.yolo_model, face_service, config.recognition, attendance_engine, store),
        media_type='multipart/x-mixed-replace; boundary=frame',
    )

# Personnel APIs
@app.post('/api/v1/personnel')
def create_person(body:PersonnelCreate,s=Depends(get_store)):
    try: return s.create_person(body)
    except Exception as e: raise HTTPException(409,str(e))

@app.get('/api/v1/personnel')
def list_people(s=Depends(get_store)):
    items = []
    for person in s.list_people():
        faces = s.list_faces(person['id'])
        primary_face = faces[0] if faces else None
        items.append({
            **person,
            'face_count': len(faces),
            'primary_face_url': _face_image_url(person['id'], primary_face['id']) if primary_face and primary_face.get('image_path') else None,
        })
    return {'items':items}

@app.get('/api/v1/personnel/{person_id}')
def get_person(person_id:str,s=Depends(get_store)):
    p=s.get_person(person_id)
    if not p: raise HTTPException(404,'Person not found')
    return p

@app.patch('/api/v1/personnel/{person_id}')
def patch_person(person_id:str,body:PersonnelPatch,s=Depends(get_store)):
    if not s.get_person(person_id): raise HTTPException(404,'Person not found')
    return s.patch_person(person_id,body.model_dump(exclude_unset=True))

@app.delete('/api/v1/personnel/{person_id}')
def deactivate_person(person_id:str,s=Depends(get_store)):
    if not s.get_person(person_id): raise HTTPException(404,'Person not found')
    return s.patch_person(person_id,{'active':False})

@app.post('/api/v1/personnel/{person_id}/faces')
async def enroll_face(person_id:str,file:UploadFile=File(...),s=Depends(get_store)):
    if not s.get_person(person_id): raise HTTPException(404,'Person not found')
    if file.content_type not in {'image/jpeg','image/png','image/webp'}: raise HTTPException(415,'Unsupported image type')
    raw=await file.read()
    if len(raw)>8*1024*1024: raise HTTPException(413,'Image too large')
    img=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
    if img is None: raise HTTPException(400,'Invalid image')
    try: emb,q=face_service.enroll(img)
    except ValueError as e: raise HTTPException(400,str(e))
    preview_path = _save_face_preview(person_id, img)
    face = s.add_face(person_id,emb,q,preview_path)
    return {
        **face,
        'image_url': _face_image_url(person_id, face['id']) if preview_path else None,
    }

@app.get('/api/v1/personnel/{person_id}/faces')
def faces(person_id:str,s=Depends(get_store)):
    return {'items':[
        {
            **face,
            'image_url': _face_image_url(person_id, face['id']) if face.get('image_path') else None,
        }
        for face in s.list_faces(person_id)
    ]}

@app.get('/api/v1/personnel/{person_id}/faces/{face_id}/image')
def face_image(person_id:str,face_id:str,s=Depends(get_store)):
    face = s.get_face(person_id, face_id)
    if not face or not face.get('image_path'):
        raise HTTPException(404,'Face image not found')
    path = Path(face['image_path'])
    if not path.exists() or not path.is_file():
        raise HTTPException(404,'Face image not found')
    return Response(content=path.read_bytes(), media_type='image/jpeg')

@app.delete('/api/v1/personnel/{person_id}/faces/{face_id}')
def delete_face(person_id:str,face_id:str,s=Depends(get_store)):
    if not s.delete_face(person_id,face_id): raise HTTPException(404,'Face not found')
    return {'deleted':True}

# Attendance APIs
@app.get('/api/v1/attendance')
def attendance(person_id:str|None=None,s=Depends(get_store)): return {'items':s.attendance(person_id)}

@app.get('/api/v1/attendance/today')
def attendance_today(s=Depends(get_store)): return {'items':s.attendance()}

@app.get('/api/v1/attendance/{person_id}')
def attendance_person(person_id:str,s=Depends(get_store)): return {'items':s.attendance(person_id)}

# Presence API
@app.get('/api/v1/presence')
def presence(s=Depends(get_store)):
    people = {person['id']: person for person in s.list_people()}
    items = []
    for item in attendance_engine.presence_list():
        person = people.get(item.get('person_id'))
        if person:
            item = {**item, 'full_name': person.get('full_name'), 'employee_code': person.get('employee_code'), 'role': person.get('role')}
        items.append(item)
    return {'items':items}

@app.get('/api/v1/person-events')
def person_events(person_id:str|None=None,s=Depends(get_store)): return {'items':s.person_events(person_id)}

# Unknown Incidents APIs
@app.get('/api/v1/unknown-incidents')
def unknowns(s=Depends(get_store)): return {'items':s.unknowns()}

@app.get('/api/v1/unknown-incidents/{incident_id}')
def unknown(incident_id:str,s=Depends(get_store)):
    i=s.unknown(incident_id)
    if not i: raise HTTPException(404,'Incident not found')
    return i

@app.post('/api/v1/unknown-incidents/{incident_id}/acknowledge')
def ack(incident_id:str,s=Depends(get_store)):
    i=s.acknowledge_unknown(incident_id)
    if not i: raise HTTPException(404,'Incident not found')
    return i
