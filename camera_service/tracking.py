from __future__ import annotations
import math

class CentroidTracker:
    """Deterministic fallback tracker for tests/dev. Production should use Ultralytics ByteTrack adapter below."""
    def __init__(self,max_distance=90): self.next_id=1; self.centers={}; self.max_distance=max_distance
    def update(self,detections):
        out=[]; used=set(); new={}
        for d in detections:
            x1,y1,x2,y2=d['bbox']; c=((x1+x2)/2,(y1+y2)/2); best=None; bd=1e9
            for tid,pc in self.centers.items():
                if tid in used: continue
                dist=math.dist(c,pc)
                if dist<bd and dist<self.max_distance: best=tid; bd=dist
            if best is None: best=self.next_id; self.next_id+=1
            used.add(best); new[best]=c; out.append({**d,'track_id':str(best)})
        self.centers=new; return out
    def reset(self): self.centers.clear()

class UltralyticsByteTracker:
    """Adapter around Ultralytics YOLO.track(persist=True, tracker='bytetrack.yaml')."""
    def __init__(self, model_path:str, classes=(0,), conf=0.35):
        from ultralytics import YOLO
        self.model=YOLO(model_path); self.classes=list(classes); self.conf=conf
    def track_frame(self,frame):
        results=self.model.track(frame,persist=True,tracker='bytetrack.yaml',classes=self.classes,conf=self.conf,verbose=False)
        out=[]
        if not results: return out
        boxes=results[0].boxes
        if boxes is None or boxes.id is None: return out
        for xyxy,tid,cf,cl in zip(boxes.xyxy.cpu().numpy(),boxes.id.int().cpu().tolist(),boxes.conf.cpu().tolist(),boxes.cls.int().cpu().tolist()):
            out.append({'bbox':tuple(float(v) for v in xyxy),'track_id':str(tid),'confidence':float(cf),'class_id':int(cl)})
        return out
    def reset(self):
        # Ultralytics creates a new tracker on next non-persisted stream; recreate worker on reconnect when needed.
        pass
