from __future__ import annotations
import threading
import cv2, numpy as np
from camera_service.bbox_utils import normalize_xyxy, width, height, area

class FaceService:
    def __init__(self, store, det_size=(640,640)):
        self.store=store; self._lock=threading.RLock(); self._app=None; self._cascade=None
        try:
            from insightface.app import FaceAnalysis
            self._app=FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider']); self._app.prepare(ctx_id=-1,det_size=det_size)
        except Exception:
            self._cascade=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
    def detect(self,frame):
        if self._app is not None:
            with self._lock: faces=self._app.get(frame)
            out=[]
            for f in faces:
                box=normalize_xyxy(tuple(float(v) for v in f.bbox)); emb=np.asarray(f.normed_embedding,dtype=np.float32) if getattr(f,'normed_embedding',None) is not None else None
                out.append({'bbox':box,'embedding':emb,'det_score':float(getattr(f,'det_score',1.0))})
            return out
        gray=cv2.cvtColor(frame,cv2.COLOR_BGR2GRAY); rects=self._cascade.detectMultiScale(gray,1.1,5,minSize=(40,40)); return [{'bbox':(x,y,x+w,y+h),'embedding':None,'det_score':0.5} for x,y,w,h in rects]
    def quality(self,face,frame_shape):
        b=face['bbox']; h,w=frame_shape[:2]; ratio=area(b)/(w*h); size=min(width(b),height(b)); score=min(1.0, max(0.0,(size-30)/170))*0.7 + min(1.0,ratio/0.05)*0.3
        return float(score)
    def recognize(self,embedding,threshold):
        if embedding is None: return None,0.0
        best=None; best_score=-1.0; e=np.asarray(embedding,dtype=np.float32); e=e/(np.linalg.norm(e)+1e-9)
        for row in self.store.embeddings():
            v=np.asarray(row['embedding'],dtype=np.float32); v=v/(np.linalg.norm(v)+1e-9); score=float(np.dot(e,v))
            if score>best_score: best_score=score; best=row
        return (best if best_score>=threshold else None), best_score
    def enroll(self,image):
        faces=self.detect(image)
        usable=[f for f in faces if self.quality(f,image.shape)>=0.35]
        if len(usable)!=1: raise ValueError(f'expected exactly one usable face, found {len(usable)}')
        if usable[0]['embedding'] is None: raise ValueError('InsightFace embedding unavailable; install insightface/onnxruntime')
        return usable[0]['embedding'].astype(float).tolist(), self.quality(usable[0],image.shape)
