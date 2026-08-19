from __future__ import annotations
import threading
from dataclasses import dataclass
from datetime import datetime
from camera_service.models import IdentitySeen, LineCrossingEvent

@dataclass
class Presence:
    person_id:str; status:str='PRESENT'; attendance_session_id:str|None=None; first_seen_today:datetime|None=None; last_seen_at:datetime|None=None; last_camera_id:str|None=None; last_confidence:float=0.0

class AttendanceEngine:
    def __init__(self, store, store_id:str, pending_window_seconds:float=5.0):
        self.store=store; self.store_id=store_id; self.pending_window_seconds=pending_window_seconds; self._lock=threading.RLock(); self.presence={}; self.identities={}; self.crossings={}
    def on_identity(self, ev:IdentitySeen):
        key=(ev.camera_id,ev.track_id)
        with self._lock:
            self.identities[key]=ev
            p=self.presence.get(ev.person_id) or Presence(person_id=ev.person_id,first_seen_today=ev.timestamp)
            p.last_seen_at=ev.timestamp; p.last_camera_id=ev.camera_id; p.last_confidence=ev.confidence; p.status='PRESENT'; self.presence[ev.person_id]=p
            cross=self.crossings.get(key)
            if cross and abs((ev.timestamp-cross.timestamp).total_seconds())<=self.pending_window_seconds: return self._apply(ev,cross)
        return None
    def on_crossing(self, ev:LineCrossingEvent):
        key=(ev.camera_id,ev.track_id)
        with self._lock:
            self.crossings[key]=ev
            ident=self.identities.get(key)
            if ident and abs((ev.timestamp-ident.timestamp).total_seconds())<=self.pending_window_seconds: return self._apply(ident,ev)
        return None
    def _apply(self, ident:IdentitySeen, cross:LineCrossingEvent):
        if cross.direction=='ENTRY':
            s,created=self.store.create_arrival(ident.person_id,self.store_id,cross.timestamp,cross.camera_id,ident.confidence)
            p=self.presence[ident.person_id]; p.attendance_session_id=s['id']; p.status='PRESENT'
            return {'type':'ARRIVAL' if created else 'PRESENCE','session':s}
        if cross.direction=='EXIT':
            s,closed=self.store.close_exit(ident.person_id,self.store_id,cross.timestamp,cross.camera_id,ident.confidence)
            p=self.presence[ident.person_id]; p.status='ABSENT'; p.attendance_session_id=None
            if not closed:
                self.store.add_person_event(ident.person_id,self.store_id,cross.camera_id,'EXIT_WITHOUT_OPEN_SESSION',cross.timestamp)
                return {'type':'EXIT_WITHOUT_OPEN_SESSION'}
            return {'type':'EXIT','session':s}
    def on_track_lost(self,camera_id,track_id):
        # Explicitly no attendance EXIT here.
        with self._lock:
            self.identities.pop((camera_id,track_id),None); self.crossings.pop((camera_id,track_id),None)
    def presence_list(self):
        return [vars(v).copy() for v in self.presence.values()]
