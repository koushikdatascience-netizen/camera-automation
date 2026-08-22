from __future__ import annotations
import threading
from dataclasses import dataclass
from datetime import datetime
from camera_service.models import IdentitySeen, LineCrossingEvent

@dataclass
class Presence:
    person_id:str; status:str='PRESENT'; attendance_session_id:str|None=None; first_seen_today:datetime|None=None; last_seen_at:datetime|None=None; last_camera_id:str|None=None; last_confidence:float=0.0; current_track_id:str|None=None; break_started_at:datetime|None=None; last_snapshot_path:str|None=None

class AttendanceEngine:
    def __init__(self, store, store_id:str, pending_window_seconds:float=5.0):
        self.store=store; self.store_id=store_id; self.pending_window_seconds=pending_window_seconds; self._lock=threading.RLock(); self.presence={}; self.identities={}; self.crossings={}
    def on_identity(self, ev:IdentitySeen):
        key=(ev.camera_id,ev.track_id)
        with self._lock:
            self.identities[key]=ev
            p=self.presence.get(ev.person_id) or Presence(person_id=ev.person_id,first_seen_today=ev.timestamp)
            if p.status=='BREAK':
                self.store.add_person_event(ev.person_id,self.store_id,ev.camera_id,'BREAK_END',ev.timestamp,{'track_id':ev.track_id,'break_started_at':p.break_started_at.isoformat() if p.break_started_at else None,'snapshot_path':ev.snapshot_path})
                p.break_started_at=None
            session, _ = self.store.create_arrival(ev.person_id,self.store_id,ev.timestamp,ev.camera_id,ev.confidence,ev.snapshot_path)
            p.attendance_session_id=session['id'] if session else p.attendance_session_id
            p.last_seen_at=ev.timestamp; p.last_camera_id=ev.camera_id; p.last_confidence=ev.confidence; p.status='PRESENT'; self.presence[ev.person_id]=p
            p.current_track_id=ev.track_id; p.last_snapshot_path=ev.snapshot_path or p.last_snapshot_path
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
            s,created=self.store.create_arrival(ident.person_id,self.store_id,cross.timestamp,cross.camera_id,ident.confidence,ident.snapshot_path)
            p=self.presence[ident.person_id]; p.attendance_session_id=s['id']; p.status='PRESENT'
            return {'type':'ARRIVAL' if created else 'PRESENCE','session':s}
        if cross.direction=='EXIT':
            s,closed=self.store.close_exit(ident.person_id,self.store_id,cross.timestamp,cross.camera_id,ident.confidence,ident.snapshot_path)
            p=self.presence[ident.person_id]; p.status='ABSENT'; p.attendance_session_id=None
            if not closed:
                self.store.add_person_event(ident.person_id,self.store_id,cross.camera_id,'EXIT_WITHOUT_OPEN_SESSION',cross.timestamp)
                return {'type':'EXIT_WITHOUT_OPEN_SESSION'}
            return {'type':'EXIT','session':s}
    def on_track_lost(self,camera_id,track_id):
        with self._lock:
            ident=self.identities.pop((camera_id,track_id),None); self.crossings.pop((camera_id,track_id),None)
            if ident:
                p=self.presence.get(ident.person_id)
                if p and p.status=='PRESENT' and p.current_track_id==track_id:
                    p.status='BREAK'; p.break_started_at=ident.timestamp; p.last_seen_at=ident.timestamp; p.last_camera_id=camera_id
                    self.store.add_person_event(ident.person_id,self.store_id,camera_id,'BREAK_START',ident.timestamp,{'track_id':track_id,'snapshot_path':p.last_snapshot_path})
    def presence_list(self):
        return [vars(v).copy() for v in self.presence.values()]
