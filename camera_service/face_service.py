from __future__ import annotations
import threading
import sys
import hashlib
import os
import time
from functools import lru_cache
from pathlib import Path
import cv2, numpy as np
from camera_service.bbox_utils import normalize_xyxy, width, height, area

def _model_files():
    bundle=Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'face_models'
    root=bundle if (bundle/'models'/'buffalo_l').exists() else Path.home()/'.insightface'
    return tuple(root/'models'/'buffalo_l'/name for name in ('det_10g.onnx','w600k_r50.onnx'))


def _model_signature(paths):
    try:
        result=[]
        for p in paths:
            stat=p.stat()
            result.append((str(p.resolve()),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns,stat.st_ino))
        return tuple(result)
    except OSError: raise ValueError('enrollment model files unavailable') from None


@lru_cache(maxsize=4)
def _fingerprint(det_size,signature):
    digest=hashlib.sha256(f'buffalo_l:{det_size}:CPU:single-face:quality-.35:v2'.encode())
    for row in signature:
        path=Path(row[0])
        with path.open('rb') as stream:
            for chunk in iter(lambda:stream.read(1024*1024), b''):
                digest.update(chunk)
    if _model_signature(tuple(Path(row[0]) for row in signature))!=signature:
        raise ValueError('model files changed during fingerprinting')
    return digest.hexdigest()


def enrollment_model_key(det_size=(640,640)):
    """Stat-based invalidation; hash each verified weights/config generation once."""
    paths=_model_files();signature=_model_signature(paths)
    return _fingerprint(tuple(det_size),signature)

enrollment_model_key.cache_clear=_fingerprint.cache_clear
enrollment_model_key.cache_info=_fingerprint.cache_info

def validated_embedding(value):
    vector=np.asarray(value,dtype=np.float32)
    if vector.shape!=(512,) or not np.isfinite(vector).all():
        raise ValueError('invalid embedding dimensions or values')
    norm=float(np.linalg.norm(vector))
    if norm<=1e-8 or not np.isfinite(norm):
        raise ValueError('invalid embedding norm')
    return (vector/norm).astype(float).tolist()

class FaceService:
    def __init__(self, store, det_size=(640,640)):
        self.store=store; self._lock=threading.RLock(); self._app=None; self._cascade=None
        self._last_inference=0.0
        self._det_size=tuple(det_size);self._model_key=None
        try:
            loaded_key=enrollment_model_key(self._det_size)
            from insightface.app import FaceAnalysis
            bundle=Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parents[1])) / 'face_models'
            root=bundle if (bundle/'models'/'buffalo_l').exists() else Path.home()/'.insightface'
            self._app=FaceAnalysis(name='buffalo_l', root=str(root), allowed_modules=['detection','recognition'], providers=['CPUExecutionProvider']); self._app.prepare(ctx_id=-1,det_size=det_size)
            if enrollment_model_key(self._det_size)!=loaded_key: raise ValueError('model changed while loading')
            self._model_key=loaded_key
        except Exception:
            self._app=None
            self._cascade=cv2.CascadeClassifier(cv2.data.haarcascades+'haarcascade_frontalface_default.xml')
    def model_provenance(self):
        if self._app is None or not self._model_key: raise ValueError('InsightFace model provenance unavailable')
        if enrollment_model_key(self._det_size)!=self._model_key:
            raise ValueError('model files changed; restart the recognition service')
        return self._model_key
    def enroll_with_provenance(self,image):
        key=self.model_provenance();embedding,quality=self.enroll(image)
        if self.model_provenance()!=key: raise ValueError('model changed during enrollment')
        return embedding,quality,key
    def detect(self,frame):
        if self._app is not None:
            # Shared model serializes cameras. Capture already drops stale frames.
            if not self._lock.acquire(timeout=0.25):
                raise RuntimeError('face inference busy')
            try:
                fps=max(0.0,float(os.getenv('SNAPKEY_FACE_MAX_FPS','0')))
                wait=max(0.0,1.0/fps-(time.monotonic()-self._last_inference)) if fps else 0.0
                if wait>0.25:
                    raise RuntimeError('face inference rate limited')
                if wait: time.sleep(wait)
                faces=self._app.get(frame)
                self._last_inference=time.monotonic()
            finally:
                self._lock.release()
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
        if getattr(self,'_model_key',None): self.model_provenance()
        e=np.asarray(validated_embedding(embedding),dtype=np.float32)
        rows=[]; vectors=[]
        for row in self.store.embeddings():
            try:
                vectors.append(validated_embedding(row['embedding'])); rows.append(row)
            except (ValueError,TypeError):
                continue
        if getattr(self,'_model_key',None): self.model_provenance()
        if not rows: return None,0.0
        scores=np.asarray(vectors,dtype=np.float32) @ e
        index=int(np.argmax(scores)); score=float(scores[index])
        # Different employees with indistinguishable templates are inconclusive.
        if score>=threshold and any(row['person_id']!=rows[index]['person_id'] and abs(float(scores[i])-score)<0.01
               for i,row in enumerate(rows)):
            raise ValueError('ambiguous employee face match')
        return (rows[index] if score>=threshold else None),score
    def enroll(self,image):
        if getattr(self,'_model_key',None): self.model_provenance()
        faces=self.detect(image)
        usable=[f for f in faces if self.quality(f,image.shape)>=0.35]
        if len(faces)!=1 or len(usable)!=1: raise ValueError(f'expected exactly one usable face, found {len(faces)}')
        if usable[0]['embedding'] is None: raise ValueError('InsightFace embedding unavailable; install insightface/onnxruntime')
        if getattr(self,'_model_key',None): self.model_provenance()
        return validated_embedding(usable[0]['embedding']), self.quality(usable[0],image.shape)
