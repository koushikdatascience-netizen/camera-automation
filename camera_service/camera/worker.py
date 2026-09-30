from __future__ import annotations
import time, threading, os
from datetime import datetime, timezone
from pathlib import Path
import cv2
from camera_service.orientation import rotate_frame
from camera_service.bbox_utils import crop
from camera_service.tracking import UltralyticsByteTracker, CentroidTracker
from camera_service.identity_engine import IdentityResolutionEngine
from camera_service.line_crossing import LineCrossingDetector
from camera_service.models import IdentitySeen, LineCrossingEvent
from camera_service.camera.sources import VideoSource
from camera_service.object_security.alerts import ObjectSecurityAlerter

class CameraWorker:
    def __init__(self,app_config,camera_config,store,face_service,attendance_engine,tracker=None,status_callback=None):
        self.app=app_config; self.camera=camera_config; self.store=store; self.face=face_service; self.attendance=attendance_engine; self.identity=IdentityResolutionEngine(app_config.recognition); self.stop_event=threading.Event(); self.source=VideoSource(camera_config.rtsp_url,camera_config.source_type)
        self.tracker=tracker; self.status_callback=status_callback; self.frames_received=0; self.ai_frames=0; self.started_at=time.monotonic(); self.last_status_at=0.0; self.reconnect_count=0
        if self.tracker is None:
            try:
                self.tracker=UltralyticsByteTracker(
                    app_config.yolo_model,
                    imgsz=int(os.environ.get("SNAPKEY_PROFILE_TRACKING_IMGSZ","384") or 384),
                    device=os.environ.get("SNAPKEY_ULTRALYTICS_DEVICE") or None,
                )
            except Exception: self.tracker=CentroidTracker()
        attendance_line=getattr(camera_config,"attendance_line",None)
        if attendance_line is None and camera_config.features.attendance and str(getattr(camera_config.camera_role,"value",camera_config.camera_role))=="ENTRANCE_EXIT":
            from types import SimpleNamespace
            # Persisted/cloud CameraConfig does not yet carry line coordinates.
            # Use the same default horizontal entrance gate as the live tracking path.
            line_y=int(os.environ.get("SNAPKEY_ATTENDANCE_LINE_Y_PX","264"))
            attendance_line=SimpleNamespace(
                x1=0,y1=line_y,x2=10000,y2=line_y,
                inside_side=os.environ.get("SNAPKEY_ATTENDANCE_INSIDE_SIDE","positive"),
                min_crossing_displacement_px=float(os.environ.get("SNAPKEY_ATTENDANCE_MIN_CROSSING_PX","12")),
            )
        self.line=LineCrossingDetector(attendance_line) if attendance_line else None
        self.last_tracks=set(); self.last_face_attempt={}
        self.alerter=ObjectSecurityAlerter()
    def process_tracks(self,frame,tracks,ts=None):
        ts=ts or datetime.now(timezone.utc); current=set()
        for tr in tracks:
            tid=str(tr['track_id']); current.add(tid); bbox=tr['bbox']
            if self.line and self.camera.features.attendance:
                direction=self.line.update(tid,bbox)
                if direction:
                    self.attendance.on_crossing(LineCrossingEvent(store_id=self.app.store_id,camera_id=self.camera.camera_id,track_id=tid,direction=direction,timestamp=ts,bbox=bbox))
            if not self.camera.features.face_recognition: continue
            st=self.identity.tracks.get((self.camera.camera_id,tid)); now=time.monotonic(); last=self.last_face_attempt.get(tid,0)
            if st and st.state=='KNOWN' and now-last<self.app.recognition.known_recheck_seconds: continue
            if now-last<0.5: continue
            self.last_face_attempt[tid]=now
            if self.face is None:
                # A persisted/cloud camera can enable face features after process
                # startup. Do not crash the continuous detector while the face
                # service is unavailable; the supervisor can inject it later.
                continue
            roi=crop(frame,bbox); faces=self.face.detect(roi)
            if not faces: continue
            best=max(faces,key=lambda f:self.face.quality(f,roi.shape)); q=self.face.quality(best,roi.shape)
            if q<self.app.recognition.minimum_face_quality: continue
            match,score=self.face.recognize(best.get('embedding'),self.app.recognition.known_threshold)
            state=self.identity.observe(self.camera.camera_id,tid,ts,match['person_id'] if match else None,score,True)
            if state.state=='KNOWN':
                snapshot_path=self._save_known_snapshot(roi,tid,ts)
                self.attendance.on_identity(IdentitySeen(
                    store_id=self.app.store_id,camera_id=self.camera.camera_id,track_id=tid,
                    person_id=state.person_id,timestamp=ts,confidence=state.confidence,bbox=bbox,
                    snapshot_path=snapshot_path,
                ))
            elif state.state=='UNKNOWN' and self.camera.features.unknown_enabled:
                self._save_unknown(frame,roi,tid,state,ts)
        lost=self.last_tracks-current
        for tid in lost:
            self.identity.forget(self.camera.camera_id,tid); self.attendance.on_track_lost(self.camera.camera_id,tid)
            if self.line: self.line.forget(tid)
        self.last_tracks=current
    def _save_known_snapshot(self,person_roi,tid,ts):
        root=Path(self.app.evidence_dir)/self.camera.camera_id
        root.mkdir(parents=True,exist_ok=True)
        path=root/f"known_{tid}_{int(ts.timestamp()*1000)}.jpg"
        return str(path) if cv2.imwrite(str(path),person_roi) else None

    def _save_unknown(self,frame,person_roi,tid,state,ts):
        root=Path(self.app.evidence_dir)/self.camera.camera_id; root.mkdir(parents=True,exist_ok=True); base=f"unknown_{tid}_{int(ts.timestamp())}"; person_path=root/f"{base}_person.jpg"; full_path=root/f"{base}_frame.jpg"; cv2.imwrite(str(person_path),person_roi); cv2.imwrite(str(full_path),frame)
        _,created=self.store.upsert_unknown(self.app.store_id,self.camera.camera_id,tid,state.first_seen,ts,ts,state.attempts,state.best_similarity,None,str(person_path),None)
        if created and self.camera.camera_role.value in {"GENERAL","SECURITY"}:
            self.alerter.alarm_beep(f"unknown:{self.camera.camera_id}:{tid}",True,1250,180,3.0)
    def run_once(self):
        if not self.source.open(): raise RuntimeError('camera source failed to open')
        ok,frame=self.source.read()
        if not ok: return False
        frame=rotate_frame(frame, getattr(self.camera, 'rotation_degrees', 0))
        if hasattr(self.tracker,'track_frame'): tracks=self.tracker.track_frame(frame)
        else: tracks=self.tracker.update([])
        self.process_tracks(frame,tracks); return True
    def _status(self, **changes):
        if self.status_callback:
            self.status_callback(self.camera.camera_id, **changes)

    def run(self):
        self._status(state="STARTING", online=False, last_error=None)
        while not self.stop_event.is_set():
            try:
                if not self.source.cap and not self.source.open():
                    self.reconnect_count += 1
                    self._status(state="RECONNECTING", online=False, reconnect_count=self.reconnect_count, last_error="Camera source failed to open")
                    self.stop_event.wait(2)
                    continue
                ok,frame=self.source.read()
                if not ok:
                    self.reconnect_count += 1
                    self._status(state="RECONNECTING", online=False, reconnect_count=self.reconnect_count, last_error="Camera frame read failed")
                    self.source.close(); self.tracker.reset(); self.stop_event.wait(1); continue
                frame=rotate_frame(frame, getattr(self.camera, 'rotation_degrees', 0))
                self.frames_received += 1
                tracks=self.tracker.track_frame(frame) if hasattr(self.tracker,'track_frame') else self.tracker.update([])
                self.process_tracks(frame,tracks)
                self.ai_frames += 1
                now=time.monotonic()
                if now-self.last_status_at >= 1.0:
                    elapsed=max(0.001,now-self.started_at)
                    self._status(
                        state="ONLINE", online=True,
                        last_frame_at=datetime.now(timezone.utc).isoformat(),
                        capture_fps=self.frames_received/elapsed,
                        ai_fps=self.ai_frames/elapsed,
                        frames_received=self.frames_received,
                        reconnect_count=self.reconnect_count,
                        last_error=None,
                    )
                    self.last_status_at=now
            except Exception as exc:
                self.reconnect_count += 1
                self._status(state="DEGRADED", online=False, reconnect_count=self.reconnect_count, last_error=str(exc))
                self.source.close(); self.stop_event.wait(1)
        self._status(state="STOPPED", online=False)
    def stop(self): self.stop_event.set(); self.source.close()
