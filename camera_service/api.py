from __future__ import annotations
from contextlib import asynccontextmanager
from datetime import datetime, timezone
import cv2, numpy as np
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends, Query, Form, Request
from fastapi.responses import JSONResponse
from urllib.parse import urlsplit
from camera_service.config import load_config, save_edge_activation
from camera_service.models import PersonnelCreate, PersonnelPatch
from camera_service.storage import SQLiteStore
from camera_service.face_service import FaceService
from camera_service.attendance_engine import AttendanceEngine
from camera_service.camera.supervisor import CameraSupervisor
from camera_service.camera_manager import CameraManager, CameraConfig, CameraStatus, CameraState
from camera_service.camera.onvif import probe_onvif, select_profile, OnvifUnavailable
from camera_service.alert_dispatcher import AlertDispatcher
from camera_service.cloud_client import CloudSyncClient
from camera_service.licensing import LicenseManager
from camera_service.sync_worker import EdgeSyncWorker
from camera_service.object_security.detector import ObjectSecurityDetector
from camera_service.object_security.model_registry import ObjectSecurityModelRegistry
from camera_service.object_security.alerts import ObjectSecurityAlerter
from camera_service.object_security.confirmation import TemporalConfirmation
from camera_service.object_security.roi import apply_roi, map_box_from_offset
from camera_service.object_security.tiling import suppress_duplicates, tiles_for_shape
from camera_service.person_model_registry import PersonModelRegistry
from typing import Optional
from pydantic import BaseModel
import json
import os
import requests
import shutil
import tempfile
import webbrowser
import threading
import time
import socket
import sys
from pathlib import Path

config=load_config()
person_model_registry=PersonModelRegistry(config)
try:
    person_model_selection=person_model_registry.resolve()
    config.yolo_model=person_model_selection["path"]
except Exception as exc:
    person_model_selection={"runtime":"PYTORCH","path":config.yolo_model,"reason":"legacy_fallback","error":str(exc),"hardware":person_model_registry.hardware}
store=SQLiteStore(config.database_path); face_service=None; attendance_engine=AttendanceEngine(store,config.store_id); supervisor=None
camera_manager=CameraManager(config.database_path)
object_security_registry=ObjectSecurityModelRegistry(config.object_security.model_storage_dir)
object_security_detector=ObjectSecurityDetector()
object_security_alerter=ObjectSecurityAlerter()
cloud_client=CloudSyncClient(config.cloud_sync)
alert_dispatcher=AlertDispatcher(config.alerts)
license_manager=LicenseManager(config.edge)
sync_worker=None

def _seed_packaged_object_security_model():
    if not getattr(sys, "frozen", False):
        return
    if object_security_registry.active_model_path("scissors"):
        return
    bundle_base=Path(getattr(sys,"_MEIPASS",Path(sys.executable).resolve().parent))
    model_path=bundle_base/"kaggle-model"/"scissors_yolo11m_960.pt"
    if not model_path.exists():
        return
    try:
        candidate=object_security_registry.create_candidate("scissors",model_path,{
            "model_name":"Scissors YOLO11m 960",
            "version":"kaggle-yolo11m-960-2026-08-27",
            "source":"Packaged model",
            "dataset_notes":"Bundled jewellery security scissors detector.",
        })
        object_security_registry.activate_candidate("scissors",candidate["id"])
    except Exception as exc:
        object_security_detector.last_error=f"Packaged scissors model seed failed: {exc}"

_seed_packaged_object_security_model()

def _camera_needs_face(camera) -> bool:
    features=getattr(camera,'features',None)
    return bool(features and (features.face_recognition or features.unknown_enabled or features.attendance))

def _should_initialize_face_service() -> bool:
    if config.features.face_recognition or config.features.attendance or config.features.unknown_enabled:
        return True
    configured = [camera for camera in config.cameras if getattr(camera, 'enabled', False)]
    try:
        persisted = [camera for camera in camera_manager.list_cameras() if getattr(camera, 'enabled', False)]
    except Exception:
        persisted = []
    return any(_camera_needs_face(camera) for camera in configured + persisted)

def get_face_service():
    global face_service
    if face_service is None:
        face_service=FaceService(store)
    return face_service

_face_init_lock = threading.Lock()


class _DeferredFaceService:
    def detect(self, frame):
        try:
            return face_service.detect(frame) if face_service is not None else []
        except Exception:
            return []
    def quality(self, face, shape):
        try:
            return face_service.quality(face, shape) if face_service is not None else 0
        except Exception:
            return 0
    def recognize(self, embedding, threshold):
        try:
            return face_service.recognize(embedding, threshold) if face_service is not None else (None, 0)
        except Exception:
            return (None, 0)


def _initialize_background_face():
    if not _face_init_lock.acquire(blocking=False):
        return
    try:
        get_face_service()
    except Exception:
        pass
    finally:
        _face_init_lock.release()


def _shared_pipeline(camera, stop_event, publish):
    needs_face = _camera_needs_face(camera)
    if needs_face and face_service is None:
        threading.Thread(target=_initialize_background_face, name="face-initializer", daemon=True).start()
    security_path = object_security_registry.active_model_path("scissors") if camera.features.object_security else None
    return camera_manager.iter_tracking_mjpeg(camera, config.yolo_model,
        _DeferredFaceService() if needs_face else None, config.recognition, attendance_engine, store,
        str(security_path) if security_path else None, max(0.55, float(config.object_security.inference.confidence or 0.55)),
        stop_event=stop_event, publish_callback=publish)


def _camera_runtime_allowed(camera):
    try:
        _enforce_runtime_license({'tracking'} | _camera_feature_names(camera.features))
        enabled = sorted(c.camera_id for c in camera_manager.list_cameras() if c.enabled)
        if camera.camera_id not in enabled[:license_manager.status().max_cameras]:
            return False
        return True
    except HTTPException:
        return False


supervisor=CameraSupervisor(config,store,face_service,attendance_engine,camera_manager=camera_manager,
                            pipeline=_shared_pipeline, allowed=_camera_runtime_allowed)
sync_worker=EdgeSyncWorker(store,cloud_client,config.edge,config.cloud_sync,license_manager,camera_manager=camera_manager,camera_supervisor=supervisor)

@asynccontextmanager
async def lifespan(app:FastAPI):
    store.configure_event_scope(config.edge.tenant_id, config.edge.company_code, config.edge.shop_id, config.edge.edge_id)
    for camera in config.cameras:
        if camera.enabled and not camera_manager.get_camera(camera.camera_id):
            camera_manager.create_camera({**camera.model_dump(), 'rtsp_url': camera.source})
    supervisor.start(); sync_worker.start()
    try:
        yield
    finally:
        sync_worker.shutdown(); supervisor.shutdown()
app=FastAPI(title='SnapKey Vision AI',lifespan=lifespan)


@app.middleware("http")
async def protect_local_origin(request: Request, call_next):
    if request.client and request.client.host not in {'127.0.0.1','::1','testclient'}:
        return JSONResponse({'detail':'Local service requires a loopback connection'}, status_code=403)
    hostname = urlsplit("//" + request.headers.get("host", "")).hostname
    if hostname not in {"localhost", "127.0.0.1", "::1", "testserver"}:
        return JSONResponse({"detail": "Invalid local service host"}, status_code=403)
    origin = request.headers.get("origin")
    if origin and origin != str(request.base_url).rstrip('/'):
        return JSONResponse({"detail": "Cross-origin local API access is denied"}, status_code=403)
    return await call_next(request)

def get_store(): return store

def _camera_feature_names(features) -> set[str]:
    data = features.model_dump() if hasattr(features, "model_dump") else dict(features or {})
    enabled = {name for name, value in data.items() if value}
    # Normalize compatibility aliases to the canonical commercial feature names.
    # A camera may persist both fields for backward compatibility, but licensing
    # must not require the same capability twice under two different names.
    if "unknown_person_detection" in enabled:
        enabled.discard("unknown_person_detection")
        enabled.add("unknown_detection")
    if "shoplifting_detection" in enabled:
        enabled.discard("shoplifting_detection")
        enabled.add("shoplifting")
    return enabled

def _merged_camera_features(camera=None, updates: dict | None = None) -> set[str]:
    if updates and "features" in updates:
        return _camera_feature_names(updates.get("features") or {})
    if camera is not None:
        return _camera_feature_names(camera.features)
    return set()

def _enforce_camera_license(features: set[str], creating: bool = False) -> None:
    status = license_manager.status()
    if status.limited_mode:
        raise HTTPException(402, {"message": status.reason, "license": status.model_dump()})
    current_cameras = len(camera_manager.list_cameras())
    if creating and current_cameras >= status.max_cameras:
        raise HTTPException(402, {"message": f"Camera limit reached for plan {status.plan}.", "license": status.model_dump()})
    denied = sorted(feature for feature in features if not status.allows_feature(feature))
    if denied:
        raise HTTPException(402, {"message": "Feature is not enabled for this license.", "features": denied, "license": status.model_dump()})

def _enforce_runtime_license(required_features: set[str] | None = None) -> None:
    status = license_manager.status()
    if status.limited_mode:
        raise HTTPException(402, {"message": status.reason, "license": status.model_dump()})
    denied = sorted(feature for feature in (required_features or set()) if not status.allows_feature(feature))
    if denied:
        raise HTTPException(402, {"message": "Feature is not enabled for this license.", "features": denied, "license": status.model_dump()})

def _validate_object_class(object_class: str) -> str:
    object_class=(object_class or "scissors").strip().lower()
    if object_class not in set(config.object_security.object_classes or ["scissors"]):
        raise HTTPException(400, f"Unsupported object class: {object_class}")
    return object_class

def _validate_uploaded_pt(path: Path):
    return object_security_detector.validate_model(path)

def _object_security_model_path(object_class: str, candidate_id: str | None = None) -> tuple[Path | None, dict | None]:
    object_class=_validate_object_class(object_class)
    if candidate_id:
        candidate=object_security_registry.candidate_path(object_class,candidate_id)
        model_path=candidate/'best.pt'
        metadata_path=candidate/'metadata.json'
        metadata=json.loads(metadata_path.read_text(encoding='utf-8')) if metadata_path.exists() else {'id':candidate_id}
        return (model_path if model_path.exists() else None), metadata
    models=object_security_registry.list_models(object_class)
    active=models.get('active')
    model_path=object_security_registry.active_model_path(object_class)
    return model_path, (active or {}).get('metadata') if active else None

def _placeholder_mjpeg(message: str):
    frame=np.zeros((480, 800, 3), dtype=np.uint8)
    cv2.putText(frame, 'SnapKey Vision AI', (30, 60), cv2.FONT_HERSHEY_SIMPLEX, 1.0, (0, 220, 255), 2)
    cv2.putText(frame, message[:72], (30, 120), cv2.FONT_HERSHEY_SIMPLEX, 0.72, (255, 255, 255), 2)
    ok, encoded=cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 75])
    if ok:
        yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"+encoded.tobytes()+b"\r\n"

def _valid_object_security_box(frame_shape, bbox, confidence: float, min_confidence: float) -> bool:
    if float(confidence) < float(min_confidence):
        return False
    h,w=frame_shape[:2]
    x1,y1,x2,y2=[float(v) for v in bbox]
    box_w=max(0.0,x2-x1); box_h=max(0.0,y2-y1)
    if box_w < 12 or box_h < 12:
        return False
    area_ratio=(box_w*box_h)/max(1.0,float(w*h))
    return 0.00035 <= area_ratio <= 0.35

def _detections_from_results(results, object_class: str, offset=(0,0), min_conf: float = 0.30) -> list[dict]:
    detections=[]
    if not results:
        return detections
    result=results[0]
    names=getattr(result,'names',{}) or {}
    boxes=result.boxes
    if boxes is None:
        return detections
    xyxy=boxes.xyxy.cpu().numpy() if boxes.xyxy is not None else []
    confs=boxes.conf.cpu().tolist() if boxes.conf is not None else []
    classes=boxes.cls.int().cpu().tolist() if boxes.cls is not None else []
    ids=boxes.id.int().cpu().tolist() if boxes.id is not None else list(range(1,len(xyxy)+1))
    for coords,conf,class_id,track_id in zip(xyxy,confs,classes,ids):
        label=str(names.get(class_id,f'class_{class_id}')).lower()
        if label != object_class.lower() or float(conf) < min_conf:
            continue
        detections.append({'bbox':map_box_from_offset(tuple(float(v) for v in coords),offset),'confidence':float(conf),'class':label,'track_id':str(track_id)})
    return detections

def _object_security_mjpeg(camera_id: str | None, source: str | None, model_path: Path, metadata: dict | None, object_class: str, conf: float, imgsz: int, roi_enabled: bool, tiled: bool, temporal: bool, beep: bool):
    source_text=source or ''
    if camera_id:
        cam=camera_manager.get_camera(camera_id)
        if not cam:
            yield from _placeholder_mjpeg('Selected camera was not found.')
            return
        source_text=cam.rtsp_url
    if not source_text:
        yield from _placeholder_mjpeg('Select a camera, RTSP URL, webcam index, or test video.')
        return
    confirmation=TemporalConfirmation(config.object_security.confirmation.window_frames,config.object_security.confirmation.required_hits,config.object_security.confirmation.minimum_confidence)
    target_delay=1.0/max(1.0,min(12.0,float(config.object_security.inference.imgsz and 6.0)))
    frame_count=0
    last_fps_at=time.monotonic()
    source_fps=0.0
    model_version=(metadata or {}).get('version') or (metadata or {}).get('id') or 'candidate'
    security_stream_state={}
    def handle_frame(frame):
        nonlocal frame_count,last_fps_at,source_fps
        started=time.monotonic()
        frame_count+=1
        now=time.monotonic()
        if now-last_fps_at>=1.0:
            source_fps=frame_count/(now-last_fps_at); frame_count=0; last_fps_at=now
        roi_dict={'enabled':roi_enabled,'x1':0,'y1':0,'x2':frame.shape[1],'y2':frame.shape[0]}
        infer_frame,offset=apply_roi(frame,roi_dict)
        detections=[]
        try:
            if tiled:
                h,w=infer_frame.shape[:2]
                for x1,y1,x2,y2 in tiles_for_shape(w,h,config.object_security.tiling.tile_size,config.object_security.tiling.overlap):
                    tile=infer_frame[y1:y2,x1:x2]
                    results=object_security_detector.predict(model_path,tile,conf=conf,imgsz=imgsz)
                    detections.extend(_detections_from_results(results,object_class,(offset[0]+x1,offset[1]+y1),conf))
                detections=suppress_duplicates(detections)
            else:
                results=object_security_detector.predict(model_path,infer_frame,conf=conf,imgsz=imgsz)
                detections=_detections_from_results(results,object_class,offset,conf)
        except Exception as exc:
            object_security_detector.last_error=str(exc)
            cv2.putText(frame,f'AI error: {str(exc)[:48]}',(20,64),cv2.FONT_HERSHEY_SIMPLEX,.6,(0,0,255),2)
            detections=[]
        latency_ms=(time.monotonic()-started)*1000
        active_tracks=set()
        alert_state='WAITING'
        for idx,det in enumerate(detections, start=1):
            x1,y1,x2,y2=[int(v) for v in det['bbox']]
            if not _valid_object_security_box(frame.shape,(x1,y1,x2,y2),det['confidence'],conf):
                continue
            track_id=det.get('track_id') or str(idx)
            active_tracks.add(track_id)
            state=confirmation.update(track_id,det['confidence']) if temporal else 'CONFIRMED'
            if state=='CONFIRMED':
                alert_state='CONFIRMED'
                key=f'{camera_id or source_text}:{object_class}:{track_id}'
                object_security_alerter.alarm_beep(f"object-security:{key}",beep,1400,160,3.0)
                if object_security_alerter.should_alert(key,config.object_security.alert.cooldown_seconds):
                    snapshot_path=None
                    if config.object_security.alert.save_snapshot:
                        evidence_dir=Path(config.evidence_dir)/(camera_id or 'object_security')
                        evidence_dir.mkdir(parents=True,exist_ok=True)
                        snapshot_path=str(evidence_dir/f'object_security_{int(time.time()*1000)}.jpg')
                        cv2.imwrite(snapshot_path,frame)
                    beep_sent=bool(beep)
                    event_time=datetime.now(timezone.utc)
                    store.create_object_security_event(camera_id or 'test_source',object_class,det['confidence'],event_time,track_id=track_id,confirmed=True,alert_sent=beep_sent,snapshot_path=snapshot_path,model_version=model_version,metadata={'bbox':[x1,y1,x2,y2]})
                    alert=store.create_security_alert(
                        config.store_id,
                        camera_id or 'test_source',
                        'SECURITY_OBJECT_ALERT',
                        object_class,
                        det['confidence'],
                        event_time,
                        snapshot_path=snapshot_path,
                        metadata={'bbox':[x1,y1,x2,y2],'source':'object_security_stream','model_version':model_version,'track_id':track_id},
                    )
                    camera_manager._begin_security_clip(security_stream_state,alert['id'],camera_id or 'object_security')
            elif state=='VERIFYING':
                alert_state='VERIFYING'
                continue
            color=(0,0,255) if state=='CONFIRMED' else (0,180,255)
            label=f"{object_class} ID {track_id} {det['confidence']:.2f} {state}"
            cv2.rectangle(frame,(x1,y1),(x2,y2),color,2)
            cv2.rectangle(frame,(x1,max(0,y1-24)),(min(frame.shape[1],x1+320),y1),color,-1)
            cv2.putText(frame,label,(x1+4,max(16,y1-7)),cv2.FONT_HERSHEY_SIMPLEX,.52,(0,0,0),2)
        confirmation.forget(active_tracks)
        top=f"{object_class.upper()} | model {model_version} | src {source_fps:.1f}fps | latency {latency_ms:.0f}ms | {alert_state}"
        cv2.putText(frame,top,(18,32),cv2.FONT_HERSHEY_SIMPLEX,.64,(255,255,255),3)
        cv2.putText(frame,top,(18,32),cv2.FONT_HERSHEY_SIMPLEX,.64,(0,0,0),1)
        camera_manager._record_security_clip_frame(security_stream_state,frame,store)
        return frame
    if camera_manager._is_dshow_source(source_text):
        for snapshot in camera_manager._iter_dshow_mjpeg_frames(source_text,fps=8):
            frame=cv2.imdecode(np.frombuffer(snapshot,dtype=np.uint8),cv2.IMREAD_COLOR)
            if frame is None: continue
            annotated=handle_frame(frame)
            ok,encoded=cv2.imencode('.jpg',annotated,[int(cv2.IMWRITE_JPEG_QUALITY),70])
            if ok: yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"+encoded.tobytes()+b"\r\n"
        return
    cap=camera_manager._open_video_capture(source_text)
    try:
        while cap.isOpened():
            loop_start=time.monotonic(); ok,frame=cap.read()
            if not ok or frame is None: break
            annotated=handle_frame(frame)
            ok,encoded=cv2.imencode('.jpg',annotated,[int(cv2.IMWRITE_JPEG_QUALITY),70])
            if ok: yield b"--frame\r\nContent-Type: image/jpeg\r\n\r\n"+encoded.tobytes()+b"\r\n"
            elapsed=time.monotonic()-loop_start
            if elapsed<target_delay: time.sleep(target_delay-elapsed)
    finally:
        camera_manager._finalize_security_clip(security_stream_state,store)
        cap.release()

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
    active_model=object_security_registry.list_models("scissors").get("active")
    return {
        'status':'ok',
        'store_id':config.store_id,
        'runtime': {
            'profile': config.runtime.profile,
            'inference_backend': os.environ.get('SNAPKEY_INFERENCE_BACKEND', 'AUTO'),
            'model': config.yolo_model,
            'person_model_runtime': person_model_selection.get('runtime'),
            'person_model_reason': person_model_selection.get('reason'),
            'person_model_diagnostic_disabled': person_model_selection.get('diagnostic_disabled', []),
            'person_model_hardware': person_model_selection.get('hardware'),
            'active_backends': [{
                'camera_id': key[1], 'backend': getattr(getattr(backend,'backend_type',None),'value','UNKNOWN'),
                'model': Path(getattr(backend,'model_path',config.yolo_model)).name,
                'fallback_reason': getattr(backend,'fallback_reason',None),
            } for key,backend in list(camera_manager._inference_backends.items()) if isinstance(key,tuple)],
            'face_recognition_ready': face_service is not None and getattr(face_service,'_app',None) is not None,
            'tracking_fps_cap': os.environ.get('SNAPKEY_PROFILE_TRACKING_FPS'),
            'tracking_imgsz_cap': os.environ.get('SNAPKEY_PROFILE_TRACKING_IMGSZ'),
            'tracking_quality_cap': os.environ.get('SNAPKEY_PROFILE_TRACKING_QUALITY'),
        },
        'cameras':len(camera_manager.list_cameras()),
        'online_cameras':sum(1 for status in statuses if status and status.online),
        'camera_workers':supervisor.status(),
        'cloud_sync':sync_worker.status(),
        'license':{'active':license_manager.status().active,'mode':license_manager.status().mode},
        'object_security_enabled':config.object_security.enabled or config.features.object_security,
        'object_security_ready':bool(active_model and active_model.get("has_model")),
        'object_security_model_loaded':object_security_detector._model is not None,
        'active_model_version':(active_model or {}).get("metadata",{}).get("version") if active_model else None,
        'last_object_security_error':object_security_detector.last_error,
    }

@app.get('/ready')
def ready():
    active_model=object_security_registry.list_models("scissors").get("active")
    return {
        'status':'ready',
        'camera_supervisor_running':supervisor.is_running(),
        'object_security_ready':bool(active_model and active_model.get("has_model")),
    }

@app.get('/api/v1/edge/status')
def edge_status():
    license_status = license_manager.status()
    return {
        'edge': config.edge.model_dump(exclude={'activation_token'}),
        'license': license_status.model_dump(),
        'cloud_sync_enabled': cloud_client.enabled(),
        'cloud_sync_allowed': cloud_client.enabled() and license_status.active and license_status.allows_feature('cloud_sync'),
        'cloud_sync_worker': sync_worker.status(),
        'runtime': {
            'profile': config.runtime.profile,
            'inference_backend': os.environ.get('SNAPKEY_INFERENCE_BACKEND', 'AUTO'),
            'model': config.yolo_model,
            'tracking_fps_cap': os.environ.get('SNAPKEY_PROFILE_TRACKING_FPS'),
            'tracking_imgsz_cap': os.environ.get('SNAPKEY_PROFILE_TRACKING_IMGSZ'),
            'tracking_quality_cap': os.environ.get('SNAPKEY_PROFILE_TRACKING_QUALITY'),
        },
        'queue': store.event_queue_status(),
        'alert_recipients': alert_dispatcher.preview_recipients(),
        'evidence': config.evidence.model_dump(),
    }

@app.post('/api/v1/edge/sync')
def sync_edge_events():
    _enforce_runtime_license({"cloud_sync"})
    return sync_worker.run_once().model_dump()

@app.get('/api/v1/license/status')
def license_status():
    return license_manager.status().model_dump()

@app.get('/api/v1/license/machine-code')
def license_machine_code():
    return {'machine_code': license_manager.machine_code()}

class LicenseInstallRequest(BaseModel):
    license: dict
    signature: str

class EdgeActivationRequest(BaseModel):
    company_code: str = ""
    shop_code: str = ""
    activation_code: str

@app.post('/api/v1/license/install')
def install_license(request: LicenseInstallRequest):
    try:
        status = license_manager.install_signed_license(request.license, request.signature)
        return status.model_dump()
    except Exception as exc:
        raise HTTPException(400, str(exc))

@app.post('/api/v1/activate')
def activate_edge(request: EdgeActivationRequest):
    global config, cloud_client, license_manager, sync_worker
    machine_code = license_manager.machine_code()
    cloud_url = "https://camera.snapkey.ai"
    try:
        response = requests.post(
            cloud_url + "/edge/v1/activate",
            json={
                "company_code": request.company_code.strip(),
                "shop_code": request.shop_code.strip(),
                "activation_code": request.activation_code.strip(),
                "machine_code": machine_code,
                "device_name": socket.gethostname(),
            },
            timeout=20,
        )
        response.raise_for_status()
        activation = response.json()
        candidate_edge = config.edge.model_copy(update={**activation["edge"],
            "activation_required": True, "license_public_key": activation["license_public_key"]})
        candidate_manager = LicenseManager(candidate_edge)
        candidate_manager._validate_signed_license(activation["license"], activation["signature"])
        if not candidate_manager._status_from_payload(activation["license"]).active:
            raise ValueError("Activation returned an expired license")
        config_path = save_edge_activation(activation)
        config = load_config()
        cloud_client.config = config.cloud_sync
        license_manager.edge = config.edge
        sync_worker.edge_config = config.edge
        sync_worker.sync_config = config.cloud_sync
        license_status = license_manager.install_signed_license(activation["license"], activation["signature"])
        store.configure_event_scope(config.edge.tenant_id, config.edge.company_code, config.edge.shop_id, config.edge.edge_id)
        sync_result = sync_worker.run_once()
        return {
            "ok": True,
            "message": activation.get("message", "Activated successfully"),
            "config_path": str(config_path),
            "edge": {
                "tenant_id": config.edge.tenant_id,
                "company_code": config.edge.company_code,
                "shop_id": config.edge.shop_id,
                "site_id": config.edge.site_id,
                "edge_id": config.edge.edge_id,
            },
            "license": license_status.model_dump(),
            "sync": sync_result.model_dump(),
        }
    except requests.HTTPError as exc:
        raise HTTPException(400, "Cloud activation was rejected. Verify the one-time code and server configuration.")
    except Exception as exc:
        raise HTTPException(400, str(exc))

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
from fastapi.responses import HTMLResponse, Response, StreamingResponse, FileResponse


def _web_asset_dir() -> Path:
    """Resolve setup UI assets in source and PyInstaller ONEDIR builds."""
    candidates: list[Path] = []

    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass) / "camera_service" / "web")
        candidates.extend([
            executable_dir / "_internal" / "camera_service" / "web",
            executable_dir / "camera_service" / "web",
        ])

    candidates.append(Path(__file__).resolve().parent / "web")

    for candidate in candidates:
        if (candidate / "setup.html").is_file():
            return candidate

    # Return the primary candidate so the /setup diagnostic reports the
    # exact path that was attempted if all candidates are unexpectedly absent.
    return candidates[0]


WEB_ASSET_DIR = _web_asset_dir()
WEB_STATIC_DIR = WEB_ASSET_DIR / "static"

# check_dir=False keeps source-only installs from failing at import time when
# optional static assets are absent. Frozen builds resolve through sys._MEIPASS.
app.mount(
    "/static",
    StaticFiles(directory=str(WEB_STATIC_DIR), check_dir=False),
    name="static",
)


@app.get("/setup", response_class=HTMLResponse)
async def setup_ui():
    setup_path = WEB_ASSET_DIR / "setup.html"
    try:
        return HTMLResponse(content=setup_path.read_text(encoding="utf-8"), status_code=200)
    except FileNotFoundError:
        return HTMLResponse(
            content=f"<h1>Setup UI not found</h1><p>Expected: {setup_path}</p>",
            status_code=404,
        )

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

class OnvifProbeRequest(BaseModel):
    host: str
    port: int = 80
    username: str = ""
    password: str = ""
    purpose: str = "ai"


@app.post('/api/v1/cameras/onvif/probe')
def probe_camera_onvif(request: OnvifProbeRequest):
    """Probe ONVIF on the edge LAN and recommend a stream profile."""
    try:
        result = probe_onvif(request.host.strip(), request.port, request.username, request.password)
        result["recommended_profile"] = select_profile(result.get("profiles") or [], request.purpose)
        return result
    except OnvifUnavailable as exc:
        raise HTTPException(501, str(exc))
    except Exception as exc:
        raise HTTPException(502, f"ONVIF camera probe failed: {exc}")


@app.post('/api/v1/cameras')
def create_camera(camera_data: CameraCreate):
    _enforce_camera_license(_camera_feature_names(camera_data.features), creating=True)
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
    rtsp_url = camera_manager._mask_rtsp_password(camera.rtsp_url)
    return {
        **camera.model_dump(),
        'rtsp_url': rtsp_url
    }

@app.patch('/api/v1/cameras/{camera_id}')
def update_camera(camera_id: str, updates: dict):
    existing = camera_manager.get_camera(camera_id)
    if not existing:
        raise HTTPException(404, 'Camera not found')
    _enforce_camera_license(_merged_camera_features(existing, updates), creating=False)
    if updates.get('rtsp_url') == camera_manager._mask_rtsp_password(existing.rtsp_url):
        updates.pop('rtsp_url', None)
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
    camera_id: str | None = None

@app.post('/api/v1/cameras/test')
def test_rtsp_connection(request: RTSPTestRequest):
    if request.camera_id:
        camera = camera_manager.get_camera(request.camera_id)
        if not camera:
            raise HTTPException(404, 'Camera not found')
        status = camera_manager.get_camera_status(request.camera_id)
        if status and status.online:
            return {'success':True,'message':'Camera runtime is receiving frames','source':camera_manager._mask_rtsp_password(camera.rtsp_url),
                'fps':status.capture_fps,'frames_received':status.frames_received,'connection_ms':0}
        return camera_manager.test_rtsp_connection(camera.rtsp_url)
    result = camera_manager.test_rtsp_connection(request.rtsp_url)
    return result

@app.get('/api/v1/cameras/test/snapshot')
def test_rtsp_snapshot(url: str = Query(...)):
    snapshot = camera_manager.read_rtsp_snapshot(url)
    if not snapshot:
        raise HTTPException(404, 'No snapshot available')
    return Response(content=snapshot, media_type='image/jpeg')

@app.post('/api/v1/cameras/test/snapshot')
def test_camera_snapshot(request: dict):
    camera_id = request.get('camera_id')
    snapshot = supervisor.snapshot(camera_id) if camera_id else camera_manager.read_rtsp_snapshot(str(request.get('rtsp_url') or ''))
    if not snapshot:
        raise HTTPException(404, 'No snapshot available')
    return Response(content=snapshot, media_type='image/jpeg', headers={'Cache-Control':'no-store'})

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
    _enforce_runtime_license(_camera_feature_names(camera.features))
    camera_manager.update_camera(camera_id, {'enabled': True})
    supervisor.reconcile()
    return {'status': 'starting', 'camera_id': camera_id}

@app.post('/api/v1/cameras/{camera_id}/stop')
def stop_camera(camera_id: str):
    camera = camera_manager.get_camera(camera_id)
    if not camera:
        raise HTTPException(404, 'Camera not found')
    camera_manager.update_camera(camera_id, {'enabled': False})
    supervisor.reconcile()
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
    snapshot = supervisor.snapshot(camera_id)
    if not snapshot:
        raise HTTPException(404, 'No snapshot available')
    return Response(content=snapshot, media_type='image/jpeg')

@app.get('/api/v1/cameras/{camera_id}/stream')
def stream_camera(camera_id: str):
    camera = camera_manager.get_camera(camera_id)
    if not camera:
        raise HTTPException(404, 'Camera not found')
    return StreamingResponse(
        supervisor.stream(camera_id, overlay=False),
        media_type='multipart/x-mixed-replace; boundary=frame',
    )

@app.get('/api/v1/cameras/{camera_id}/tracking-stream')
def stream_camera_tracking(camera_id: str):
    camera = camera_manager.get_camera(camera_id)
    if not camera:
        raise HTTPException(404, 'Camera not found')
    _enforce_runtime_license({"tracking"} | _camera_feature_names(camera.features))
    return StreamingResponse(
        supervisor.stream(camera_id, overlay=True),
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
    try: emb,q=get_face_service().enroll(img)
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

class BreakActionRequest(BaseModel):
    camera_id: str = "manual"
    break_master_id: str | None = None

@app.post('/api/v1/personnel/{person_id}/break/start')
def start_person_break(person_id: str, body: BreakActionRequest, s=Depends(get_store)):
    if not s.get_person(person_id):
        raise HTTPException(404, 'Person not found')
    try:
        return attendance_engine.start_break(person_id, body.camera_id, break_master_id=body.break_master_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))

@app.post('/api/v1/personnel/{person_id}/break/end')
def end_person_break(person_id: str, body: BreakActionRequest, s=Depends(get_store)):
    if not s.get_person(person_id):
        raise HTTPException(404, 'Person not found')
    try:
        return attendance_engine.end_break(person_id, body.camera_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc))

# Attendance APIs
@app.get('/api/v1/attendance')
def attendance(person_id:str|None=None,limit:int|None=Query(default=None,ge=1,le=1000),camera_id:str|None=None,s=Depends(get_store)):
    return {'items':s.attendance(person_id,limit=limit,camera_id=camera_id)}

def _saved_evidence(path_value, s, media_type='image/jpeg'):
    if not path_value:
        raise HTTPException(404,'Snapshot unavailable')
    path=Path(path_value).resolve()
    roots=(Path(config.evidence_dir).resolve(), (Path(s.path).parent/'evidence').resolve())
    if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
        raise HTTPException(404,'Snapshot unavailable')
    return FileResponse(path,media_type=media_type,headers={'Cache-Control':'no-store'})

@app.get('/api/v1/attendance/{session_id}/snapshots/{side}')
def attendance_snapshot(session_id:str,side:str,s=Depends(get_store)):
    if side not in {'arrival','exit'}:
        raise HTTPException(404,'Snapshot unavailable')
    record=s.get_attendance_id(session_id)
    if not record:
        raise HTTPException(404,'Attendance record not found')
    return _saved_evidence(record.get(f'{side}_snapshot'),s)

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

# Object Security APIs
@app.get('/api/v1/object-security/status')
def object_security_status(object_class:str='scissors'):
    object_class=_validate_object_class(object_class)
    models=object_security_registry.list_models(object_class)
    active=models.get('active')
    return {
        'enabled':config.object_security.enabled or config.features.object_security,
        'object_class':object_class,
        'ready':bool(active and active.get('has_model')),
        'model_loaded':object_security_detector._model is not None,
        'active_model':active,
        'previous_model':models.get('previous'),
        'candidate_count':len(models.get('candidates',[])),
        'message':None if active else 'No custom scissors model installed.',
        'last_error':object_security_detector.last_error,
        'config':config.object_security.model_dump(exclude={'model_storage_dir'}),
    }

@app.get('/api/v1/object-security/models')
def object_security_models(object_class:str='scissors'):
    return object_security_registry.list_models(_validate_object_class(object_class))

@app.post('/api/v1/object-security/models/upload')
async def upload_object_security_model(
    file:UploadFile=File(...),
    object_class:str=Form('scissors'),
    model_name:str|None=Form(None),
    version:str|None=Form(None),
    source:str|None=Form(None),
    dataset_notes:str|None=Form(None),
    precision:float|None=Form(None),
    recall:float|None=Form(None),
    map50:float|None=Form(None),
    map50_95:float|None=Form(None),
):
    object_class=_validate_object_class(object_class)
    filename=Path(file.filename or '').name
    if not filename.lower().endswith('.pt'):
        raise HTTPException(400,'Only externally trained .pt model files are accepted.')
    max_bytes=250*1024*1024
    Path(config.object_security.model_storage_dir).mkdir(parents=True, exist_ok=True)
    temp_dir=Path(tempfile.mkdtemp(prefix='object_security_upload_', dir=config.object_security.model_storage_dir))
    temp_path=temp_dir/'best.pt'
    total=0
    try:
        with temp_path.open('wb') as out:
            while True:
                chunk=await file.read(1024*1024)
                if not chunk: break
                total+=len(chunk)
                if total>max_bytes: raise HTTPException(413,'Model upload exceeds the 250 MB limit.')
                out.write(chunk)
        try:
            validation=_validate_uploaded_pt(temp_path)
        except Exception as exc:
            raise HTTPException(400,f'Model validation failed: {exc}')
        candidate=object_security_registry.create_candidate(object_class,temp_path,{
            'model_name':model_name,'version':version,'source':source,'dataset_notes':dataset_notes,
            'precision':precision,'recall':recall,'map50':map50,'map50_95':map50_95,
        })
        return {'success':True,'candidate':candidate,'validation':validation,'activated':False}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.post('/api/v1/object-security/models/candidates/{candidate_id}/activate')
def activate_object_security_candidate(candidate_id:str,object_class:str='scissors'):
    object_class=_validate_object_class(object_class)
    try:
        models=object_security_registry.activate_candidate(object_class,candidate_id,_validate_uploaded_pt)
        object_security_detector.unload()
        return {'success':True,'models':models}
    except Exception as exc:
        raise HTTPException(400,f'Activation failed: {exc}')

@app.post('/api/v1/object-security/models/rollback')
def rollback_object_security_model(object_class:str='scissors'):
    object_class=_validate_object_class(object_class)
    try:
        models=object_security_registry.rollback(object_class,_validate_uploaded_pt)
        object_security_detector.unload()
        return {'success':True,'models':models}
    except Exception as exc:
        raise HTTPException(400,f'Rollback failed: {exc}')

@app.delete('/api/v1/object-security/models/candidates/{candidate_id}')
def delete_object_security_candidate(candidate_id:str,object_class:str='scissors'):
    deleted=object_security_registry.delete_candidate(_validate_object_class(object_class),candidate_id)
    if not deleted: raise HTTPException(404,'Candidate model not found')
    return {'deleted':True}

@app.get('/api/v1/object-security/test-stream')
def object_security_test_stream(
    camera_id:str|None=None,
    source:str|None=None,
    object_class:str='scissors',
    candidate_id:str|None=None,
    confidence:float=0.55,
    imgsz:int=960,
    roi_enabled:bool=False,
    tiled:bool=False,
    temporal:bool=True,
    beep:bool=True,
):
    object_class=_validate_object_class(object_class)
    model_path,metadata=_object_security_model_path(object_class,candidate_id)
    if not model_path:
        return StreamingResponse(_placeholder_mjpeg('No custom scissors model installed. Upload and activate Kaggle best.pt.'),media_type='multipart/x-mixed-replace; boundary=frame')
    return StreamingResponse(
        _object_security_mjpeg(camera_id,source,model_path,metadata,object_class,max(0.05,min(float(confidence),0.95)),max(320,min(int(imgsz),1280)),roi_enabled,tiled,temporal,beep),
        media_type='multipart/x-mixed-replace; boundary=frame'
    )

@app.get('/api/v1/object-security/events')
def object_security_events(s=Depends(get_store)): return {'items':s.object_security_events()}

# Security Alert APIs
@app.get('/api/v1/security-alerts')
def security_alerts(limit:int|None=Query(default=None,ge=1,le=1000),s=Depends(get_store)): return {'items':s.security_alerts(limit=limit)}

@app.get('/api/v1/security-alerts/{alert_id}')
def security_alert(alert_id:str,s=Depends(get_store)):
    alert=s.security_alert(alert_id)
    if not alert: raise HTTPException(404,'Security alert not found')
    return alert

@app.get('/api/v1/security-alerts/{alert_id}/snapshot')
def security_alert_snapshot(alert_id:str,s=Depends(get_store)):
    alert=s.security_alert(alert_id)
    if not alert or not alert.get('snapshot_path'): raise HTTPException(404,'Snapshot not found')
    path=Path(alert['snapshot_path'])
    if not path.exists() or not path.is_file(): raise HTTPException(404,'Snapshot not found')
    return Response(content=path.read_bytes(),media_type='image/jpeg')

@app.get('/api/v1/security-alerts/{alert_id}/clip')
def security_alert_clip(alert_id:str,s=Depends(get_store)):
    alert=s.security_alert(alert_id)
    if not alert or not alert.get('clip_path'): raise HTTPException(404,'Clip not found')
    path=Path(alert['clip_path'])
    if not path.exists() or not path.is_file(): raise HTTPException(404,'Clip not found')
    return Response(content=path.read_bytes(),media_type='video/mp4')

@app.post('/api/v1/security-alerts/{alert_id}/acknowledge')
def acknowledge_security_alert(alert_id:str,s=Depends(get_store)):
    alert=s.acknowledge_security_alert(alert_id)
    if not alert: raise HTTPException(404,'Security alert not found')
    return alert

# Unknown Incidents APIs
@app.get('/api/v1/unknown-incidents')
def unknowns(limit:int|None=Query(default=None,ge=1,le=1000),s=Depends(get_store)): return {'items':s.unknowns(limit=limit)}

@app.get('/api/v1/unknown-incidents/{incident_id}/snapshot')
def unknown_snapshot(incident_id:str,s=Depends(get_store)):
    incident=s.unknown(incident_id)
    if not incident:
        raise HTTPException(404,'Incident not found')
    return _saved_evidence(incident.get('best_face_snapshot') or incident.get('best_person_snapshot'),s)

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
