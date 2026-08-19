from __future__ import annotations
import time, threading
from datetime import datetime, timezone
from pathlib import Path
import cv2
from camera_service.bbox_utils import crop
from camera_service.tracking import UltralyticsByteTracker, CentroidTracker
from camera_service.identity_engine import IdentityResolutionEngine
from camera_service.line_crossing import LineCrossingDetector
from camera_service.models import IdentitySeen, LineCrossingEvent
from camera_service.camera.sources import VideoSource

class CameraWorker:
    def __init__(self,app_config,camera_config,store,face_service,attendance_engine,tracker=None):
        self.app=app_config; self.camera=camera_config; self.store=store; self.face=face_service; self.attendance=attendance_engine; self.identity=IdentityResolutionEngine(app_config.recognition); self.stop_event=threading.Event(); self.source=VideoSource(camera_config.source,camera_config.source_type)
        self.tracker=tracker
        if self.tracker is None:
            try: self.tracker=UltralyticsByteTracker(app_config.yolo_model)
            except Exception: self.tracker=CentroidTracker()
        self.line=LineCrossingDetector(camera_config.attendance_line) if camera_config.attendance_line else None
        self.last_tracks=set(); self.last_face_attempt={}
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
            roi=crop(frame,bbox); faces=self.face.detect(roi)
            if not faces: continue
            best=max(faces,key=lambda f:self.face.quality(f,roi.shape)); q=self.face.quality(best,roi.shape)
            if q<self.app.recognition.minimum_face_quality: continue
            match,score=self.face.recognize(best.get('embedding'),self.app.recognition.known_threshold)
            state=self.identity.observe(self.camera.camera_id,tid,ts,match['person_id'] if match else None,score,True)
            if state.state=='KNOWN':
                self.attendance.on_identity(IdentitySeen(store_id=self.app.store_id,camera_id=self.camera.camera_id,track_id=tid,person_id=state.person_id,timestamp=ts,confidence=state.confidence,bbox=bbox))
            elif state.state=='UNKNOWN' and self.camera.features.unknown_detection:
                self._save_unknown(frame,roi,tid,state,ts)
        lost=self.last_tracks-current
        for tid in lost:
            self.identity.forget(self.camera.camera_id,tid); self.attendance.on_track_lost(self.camera.camera_id,tid)
            if self.line: self.line.forget(tid)
        self.last_tracks=current
    def _save_unknown(self,frame,person_roi,tid,state,ts):
        root=Path(self.app.evidence_dir)/self.camera.camera_id; root.mkdir(parents=True,exist_ok=True); base=f"unknown_{tid}_{int(ts.timestamp())}"; person_path=root/f"{base}_person.jpg"; full_path=root/f"{base}_frame.jpg"; cv2.imwrite(str(person_path),person_roi); cv2.imwrite(str(full_path),frame)
        self.store.upsert_unknown(self.app.store_id,self.camera.camera_id,tid,state.first_seen,ts,ts,state.attempts,state.best_similarity,None,str(person_path),None)
    def run_once(self):
        if not self.source.open(): raise RuntimeError('camera source failed to open')
        ok,frame=self.source.read()
        if not ok: return False
        if hasattr(self.tracker,'track_frame'): tracks=self.tracker.track_frame(frame)
        else: tracks=self.tracker.update([])
        self.process_tracks(frame,tracks); return True
    def run(self):
        while not self.stop_event.is_set():
            try:
                if not self.source.cap and not self.source.open(): time.sleep(2); continue
                ok,frame=self.source.read()
                if not ok:
                    self.source.close(); self.tracker.reset(); time.sleep(1); continue
                tracks=self.tracker.track_frame(frame) if hasattr(self.tracker,'track_frame') else self.tracker.update([])
                self.process_tracks(frame,tracks)
            except Exception:
                self.source.close(); time.sleep(1)
    def stop(self): self.stop_event.set(); self.source.close()
