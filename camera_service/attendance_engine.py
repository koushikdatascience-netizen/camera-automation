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
    def on_identity(self, ev: IdentitySeen):
        """A recognized employee confirms arrival in AUTO mode without line crossing.

        An existing open session is reused by storage, preventing repeat entries.
        Manual mode continues recording observations without confirming arrival.
        """
        import os
        mode = os.environ.get("CAMERA_EYE_ATTENDANCE_MODE", "AUTO").strip().upper()
        with self._lock:
            self.identities[(ev.camera_id, ev.track_id)] = ev
            p = self.presence.get(ev.person_id) or Presence(person_id=ev.person_id, first_seen_today=ev.timestamp)
            if mode == "AUTO" and p.status != "BREAK":
                session, _ = self.store.create_arrival(
                    ev.person_id, self.store_id, ev.timestamp, ev.camera_id,
                    ev.confidence, ev.snapshot_path, confirmed=True,
                )
            else:
                session, _ = self.store.create_arrival(
                    ev.person_id, self.store_id, ev.timestamp, ev.camera_id,
                    ev.confidence, ev.snapshot_path, confirmed=False,
                )
            p.attendance_session_id = session["id"] if session else p.attendance_session_id
            p.last_seen_at = ev.timestamp
            p.last_camera_id = ev.camera_id
            p.last_confidence = ev.confidence
            if p.status != "BREAK":
                p.status = "PRESENT"
            p.current_track_id = ev.track_id
            p.last_snapshot_path = ev.snapshot_path or p.last_snapshot_path
            self.presence[ev.person_id] = p
        return None

    def start_break(self, person_id: str, camera_id: str, timestamp: datetime | None = None, break_master_id: str | None = None):
        """Create a business-confirmed break event.

        This is intentionally separate from tracking lifecycle events so CRM break
        state can only change from an explicit break action/zone workflow.
        """
        ts = timestamp or datetime.now().astimezone()
        with self._lock:
            p = self.presence.get(person_id)
            if not p or p.status != 'PRESENT':
                raise ValueError('person must be present before starting a break')
            p.status = 'BREAK'
            p.break_started_at = ts
            p.last_seen_at = ts
            p.last_camera_id = camera_id
            metadata = {'crm_confirmed_break': True}
            if break_master_id:
                metadata['break_master_id'] = break_master_id
            event_id = self.store.add_person_event(person_id,self.store_id,camera_id,'BREAK_START',ts,metadata)
            return {'event_id':event_id,'person_id':person_id,'status':'BREAK','started_at':ts.isoformat()}

    def end_break(self, person_id: str, camera_id: str, timestamp: datetime | None = None):
        ts = timestamp or datetime.now().astimezone()
        with self._lock:
            p = self.presence.get(person_id)
            if not p or p.status != 'BREAK':
                raise ValueError('person is not currently on break')
            started = p.break_started_at
            p.status = 'PRESENT'
            p.break_started_at = None
            p.last_seen_at = ts
            p.last_camera_id = camera_id
            event_id = self.store.add_person_event(person_id,self.store_id,camera_id,'BREAK_END',ts,{
                'crm_confirmed_break': True,
                'break_started_at': started.isoformat() if started else None,
            })
            return {'event_id':event_id,'person_id':person_id,'status':'PRESENT','ended_at':ts.isoformat()}

    def on_track_lost(self,camera_id,track_id):
        """Forget transient tracking state without changing business attendance state.

        A lost tracker can mean occlusion, dropped frames, camera reconnect, or a person
        simply leaving this camera's field of view. It is not evidence that an employee
        started a CRM break. Breaks must be emitted by an explicit, confirmed break
        workflow (for example a configured break zone/camera or portal action).
        """
        with self._lock:
            ident=self.identities.pop((camera_id,track_id),None)
            self.crossings.pop((camera_id,track_id),None)
            if ident:
                p=self.presence.get(ident.person_id)
                if p and p.current_track_id==track_id:
                    p.current_track_id=None
                    p.last_seen_at=ident.timestamp
                    p.last_camera_id=camera_id
    def presence_list(self):
        return [vars(v).copy() for v in self.presence.values()]
