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
from typing import Optional
from pydantic import BaseModel
import json
import os
import webbrowser
import threading
import time
import socket
import sys

config=load_config(); store=SQLiteStore(config.database_path); face_service=FaceService(store); attendance_engine=AttendanceEngine(store,config.store_id); supervisor=CameraSupervisor(config,store,face_service,attendance_engine)
camera_manager=CameraManager(config.database_path)

@asynccontextmanager
async def lifespan(app:FastAPI):
    supervisor.start(); yield; supervisor.shutdown()
app=FastAPI(title='Camera Automation Production P0',lifespan=lifespan)

def get_store(): return store

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
def list_people(s=Depends(get_store)): return {'items':s.list_people()}

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
    return s.add_face(person_id,emb,q)

@app.get('/api/v1/personnel/{person_id}/faces')
def faces(person_id:str,s=Depends(get_store)): return {'items':s.list_faces(person_id)}

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
