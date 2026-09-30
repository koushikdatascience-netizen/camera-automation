"""Local operator confirmation, separate from automatic tracking observations."""
from datetime import datetime, timezone, timedelta
import json
import threading
import uuid
from urllib.parse import quote


class AttendanceStation:
    candidate_seconds = 15
    actions = {'OUT': ['CHECK_IN'], 'IN': ['CHECK_OUT', 'START_BREAK'],
               'ON_BREAK': ['END_BREAK', 'CHECK_OUT']}

    def __init__(self, engine):
        self.engine = engine
        self._lock = threading.RLock()
        self._candidates = {}

    def candidate(self, camera_id, now=None):
        now = now or datetime.now(timezone.utc)
        with self.engine._lock:
            for key, value in list(self._candidates.items()):
                if (now - value['seen_at']).total_seconds() > self.candidate_seconds:
                    self._candidates.pop(key, None)
            identities = [ev for (cam, _), ev in self.engine.identities.items()
                          if cam == camera_id and 0 <= (now - ev.timestamp).total_seconds() <= self.candidate_seconds]
            if not identities:
                self._candidates.pop(camera_id, None)
                return None
            ev = max(identities, key=lambda item: item.timestamp)
            person = self.engine.store.get_person(ev.person_id)
            if not person or not person['active']:
                return None
            current = self._candidates.get(camera_id)
            if not current or current['person_id'] != ev.person_id:
                current = {'person_id': ev.person_id, 'token': str(uuid.uuid4())}
                self._candidates[camera_id] = current
            current['seen_at'] = ev.timestamp
            session = self.engine.store.open_session(ev.person_id, self.engine.store_id)
            state = 'OUT'
            if session and session.get('entry_confirmed'):
                state = 'ON_BREAK' if session.get('break_started_at') else 'IN'
            return {'person_id': ev.person_id, 'full_name': person['full_name'],
                    'employee_code': person['employee_code'], 'confidence': ev.confidence,
                    'detected_at': ev.timestamp.isoformat(),
                    'expires_at': (ev.timestamp + timedelta(seconds=self.candidate_seconds)).isoformat(),
                    'state': state, 'actions': self.actions[state],
                    'token': current['token'],
                    'snapshot_url': f'/api/v1/cameras/{quote(camera_id, safe="")}/attendance-station/snapshot' if ev.snapshot_path else None}

    def apply(self, camera_id, person_id, token, action, expected_state, request_id, now=None):
        now = now or datetime.now(timezone.utc)
        store = self.engine.store
        # Share the engine/store locks with automatic arrivals and exits.
        with self._lock, self.engine._lock, store._lock, store._conn() as c:
            candidate = self.candidate(camera_id, now)
            if not candidate or candidate['person_id'] != person_id or candidate['token'] != token:
                raise ValueError('Recognition expired or changed; wait for a fresh recognition')
            previous = c.execute('SELECT * FROM person_events WHERE id=?', ('manual:' + request_id,)).fetchone()
            if previous:
                metadata = json.loads(previous['metadata_json'])
                if previous['person_id'] != person_id or previous['camera_id'] != camera_id or metadata.get('action') != action:
                    raise ValueError('Request identifier already used')
                return {'applied': False, 'duplicate': True}
            if candidate['state'] != expected_state or action not in candidate['actions']:
                raise ValueError('Attendance state changed; refresh before confirming')
            ev = max((ev for (cam, _), ev in self.engine.identities.items() if cam == camera_id), key=lambda ev: ev.timestamp)
            session = c.execute("SELECT * FROM attendance_sessions WHERE person_id=? AND store_id=? AND status='OPEN' ORDER BY arrival_time DESC LIMIT 1", (person_id, self.engine.store_id)).fetchone()
            stamp = now.isoformat()
            if action == 'CHECK_IN':
                if session:
                    c.execute('UPDATE attendance_sessions SET entry_confirmed=1,arrival_time=?,arrival_camera=?,arrival_confidence=?,arrival_snapshot=? WHERE id=?',
                              (stamp, camera_id, ev.confidence, ev.snapshot_path, session['id']))
                else:
                    c.execute("INSERT INTO attendance_sessions(id,person_id,store_id,arrival_time,arrival_camera,arrival_confidence,arrival_snapshot,status,entry_confirmed) VALUES(?,?,?,?,?,?,?,'OPEN',1)",
                              (str(uuid.uuid4()), person_id, self.engine.store_id, stamp, camera_id, ev.confidence, ev.snapshot_path))
            elif action == 'CHECK_OUT':
                c.execute("UPDATE attendance_sessions SET exit_time=?,exit_camera=?,exit_confidence=?,exit_snapshot=?,status='CLOSED',break_started_at=NULL WHERE id=?",
                          (stamp, camera_id, ev.confidence, ev.snapshot_path, session['id']))
            elif action == 'START_BREAK':
                c.execute('UPDATE attendance_sessions SET break_started_at=?,last_break_start=?,last_break_end=NULL WHERE id=?', (stamp, stamp, session['id']))
            elif action == 'END_BREAK':
                c.execute('UPDATE attendance_sessions SET break_started_at=NULL,last_break_end=? WHERE id=?', (stamp, session['id']))
            c.execute('INSERT INTO person_events VALUES(?,?,?,?,?,?,?)',
                      ('manual:' + request_id, person_id, self.engine.store_id, camera_id,
                       'MANUAL_' + action, stamp, json.dumps({'action': action, 'source': 'local_attendance_station', 'prior_state': expected_state, 'candidate_token': token})))
            # Recognition remains an observation; these records are deliberately local.
            return {'applied': True, 'duplicate': False}
