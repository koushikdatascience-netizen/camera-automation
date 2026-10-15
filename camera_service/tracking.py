from __future__ import annotations
import math
from pathlib import Path


class CentroidTracker:
    """Deterministic fallback tracker for tests/dev."""
    def __init__(self,max_distance=90): self.next_id=1; self.centers={}; self.max_distance=max_distance
    def update(self,detections):
        out=[]; used=set(); new={}
        for d in detections:
            x1,y1,x2,y2=d['bbox']; center=((x1+x2)/2,(y1+y2)/2); best=None; best_distance=1e9
            for tid,previous in self.centers.items():
                if tid in used: continue
                distance=math.dist(center,previous)
                if distance<best_distance and distance<self.max_distance: best=tid; best_distance=distance
            if best is None: best=self.next_id; self.next_id+=1
            used.add(best); new[best]=center; out.append({**d,'track_id':str(best)})
        self.centers=new; return out
    def reset(self): self.centers.clear()


class UltralyticsByteTracker:
    """Model-family-neutral Ultralytics tracker.

    Any Ultralytics-compatible detection artifact (.pt, .onnx, OpenVINO
    directory, etc.) can be selected by configuration without changing the
    camera pipeline.
    """
    def __init__(self, model_path:str, classes=(0,), conf=0.35, imgsz=None, device=None):
        from ultralytics import YOLO
        self.model_path=str(model_path)
        self.model=YOLO(self.model_path)
        self.classes=list(classes)
        self.conf=float(conf)
        self.imgsz=int(imgsz) if imgsz else None
        self.device=device
        suffix=Path(self.model_path).suffix.lower()
        self.runtime=("OPENVINO" if Path(self.model_path).is_dir() or suffix==".xml" else "ONNX" if suffix==".onnx" else "PYTORCH")

    def track_frame(self,frame):
        kwargs={
            'persist':True,
            'tracker':'bytetrack.yaml',
            'classes':self.classes,
            'conf':self.conf,
            'verbose':False,
        }
        if self.imgsz: kwargs['imgsz']=self.imgsz
        # Ultralytics exported CPU artifacts select their own execution provider.
        # Passing torch device strings to ONNX/OpenVINO can be invalid.
        if self.device is not None and self.runtime=="PYTORCH": kwargs['device']=self.device
        results=self.model.track(frame,**kwargs)
        out=[]
        if not results: return out
        boxes=results[0].boxes
        if boxes is None or boxes.id is None: return out
        for xyxy,tid,cf,cl in zip(boxes.xyxy.cpu().numpy(),boxes.id.int().cpu().tolist(),boxes.conf.cpu().tolist(),boxes.cls.int().cpu().tolist()):
            out.append({'bbox':tuple(float(v) for v in xyxy),'track_id':str(tid),'confidence':float(cf),'class_id':int(cl)})
        return out

    def reset(self):
        pass
