from __future__ import annotations
from camera_service.bbox_utils import bottom_center

class LineCrossingDetector:
    def __init__(self, cfg): self.cfg=cfg; self.state={}
    def _side(self,p):
        x,y=p; x1,y1,x2,y2=self.cfg.x1,self.cfg.y1,self.cfg.x2,self.cfg.y2
        return (x2-x1)*(y-y1)-(y2-y1)*(x-x1)
    def update(self,track_id,bbox):
        p=bottom_center(bbox); side=self._side(p); prev=self.state.get(track_id); self.state[track_id]=(side,p)
        if not prev: return None
        pside,pp=prev
        if side==0 or pside==0 or side*pside>=0: return None
        disp=((p[0]-pp[0])**2+(p[1]-pp[1])**2)**0.5
        if disp<self.cfg.min_crossing_displacement_px: return None
        now_inside = side>0 if self.cfg.inside_side=='positive' else side<0
        was_inside = pside>0 if self.cfg.inside_side=='positive' else pside<0
        if not was_inside and now_inside: return 'ENTRY'
        if was_inside and not now_inside: return 'EXIT'
        return None
    def forget(self,track_id): self.state.pop(track_id,None)
