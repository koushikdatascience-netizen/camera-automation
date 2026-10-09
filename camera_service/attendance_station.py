"""Human-confirmed local attendance workstation.

Recognition is an observation. Attendance state changes happen only after an
operator confirms a fresh known-person candidate.
"""
from datetime import datetime, timezone, timedelta
import json
import threading
import uuid


class AttendanceStation:
    candidate_seconds = 15
    actions = {
        "OUT": ["CHECK_IN"],
        "IN": ["CHECK_OUT", "START_BREAK"],
        "ON_BREAK": ["END_BREAK", "CHECK_OUT"],
    }

    def __init__(self, engine, camera_manager=None):
        self.engine = engine
        self.camera_manager = camera_manager
        self._lock = threading.RLock()
        self._candidates = {}

    def _attendance_enabled(self, camera_id):
        if self.camera_manager is None:
            return True
        camera = self.camera_manager.get_camera(camera_id)
        return bool(
            camera
            and getattr(camera.camera_role, "value", camera.camera_role) == "ENTRANCE_EXIT"
            and camera.enabled
            and getattr(camera, "attendance_active", True)
            and camera.features.attendance
        )

    def candidate(self, camera_id, now=None):
        now = now or datetime.now(timezone.utc)
        if not self._attendance_enabled(camera_id):
            self._candidates.pop(camera_id, None)
            return None
        with self.engine._lock:
            for key, value in list(self._candidates.items()):
                if (now - value["seen_at"]).total_seconds() > self.candidate_seconds:
                    self._candidates.pop(key, None)
            identities = [
                ev for (cam, _), ev in self.engine.identities.items()
                if cam == camera_id
                and 0 <= (now - ev.timestamp).total_seconds() <= self.candidate_seconds
            ]
            if not identities:
                self._candidates.pop(camera_id, None)
                return None
            ev = max(identities, key=lambda item: item.timestamp)
            person = self.engine.store.get_person(ev.person_id)
            if not person or not person["active"]:
                return None
            current = self._candidates.get(camera_id)
            if not current or current["person_id"] != ev.person_id:
                current = {"person_id": ev.person_id, "token": str(uuid.uuid4())}
                self._candidates[camera_id] = current
            current["seen_at"] = ev.timestamp
            session = self.engine.store.open_session(ev.person_id, self.engine.store_id)
            state = "OUT"
            if session and session.get("entry_confirmed"):
                state = "ON_BREAK" if session.get("break_started_at") else "IN"
            return {
                "person_id": ev.person_id,
                "full_name": person["full_name"],
                "employee_code": person["employee_code"],
                "role": person.get("role"),
                "confidence": ev.confidence,
                "detected_at": ev.timestamp.isoformat(),
                "expires_at": (ev.timestamp + timedelta(seconds=self.candidate_seconds)).isoformat(),
                "state": state,
                "actions": self.actions[state],
                "token": current["token"],
                "recognition_snapshot_path": ev.snapshot_path,
            }

    def apply(
        self, camera_id, person_id, token, action, expected_state, request_id,
        now=None, evidence_path=None,
    ):
        now = now or datetime.now(timezone.utc)
        if not self._attendance_enabled(camera_id):
            raise ValueError("Attendance camera is stopped")
        store = self.engine.store
        with self._lock, self.engine._lock, store._lock, store._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            previous = c.execute(
                "SELECT * FROM person_events WHERE id=?", ("manual:" + request_id,)
            ).fetchone()
            if previous:
                metadata = json.loads(previous["metadata_json"])
                if (
                    previous["person_id"] != person_id
                    or previous["camera_id"] != camera_id
                    or metadata.get("action") != action
                ):
                    raise ValueError("Request identifier already used")
                return {"applied": False, "duplicate": True,
                        "session_id": metadata.get("attendance_session_id")}
            candidate = self.candidate(camera_id, now)
            if not candidate or candidate["person_id"] != person_id or candidate["token"] != token:
                raise ValueError("Recognition expired or changed; wait for a fresh recognition")
            if candidate["state"] != expected_state or action not in candidate["actions"]:
                raise ValueError("Attendance state changed; refresh before confirming")

            ev = max(
                (
                    ev for (cam, _), ev in self.engine.identities.items()
                    if cam == camera_id and ev.person_id == person_id
                ),
                key=lambda ev: ev.timestamp,
            )
            session = c.execute(
                "SELECT * FROM attendance_sessions WHERE person_id=? AND store_id=? "
                "AND status='OPEN' ORDER BY arrival_time DESC LIMIT 1",
                (person_id, self.engine.store_id),
            ).fetchone()
            stamp = now.isoformat()
            action_snapshot = evidence_path or ev.snapshot_path
            session_id = session["id"] if session else str(uuid.uuid4())
            recognized = c.execute("SELECT id,metadata_json,event_time FROM person_events WHERE person_id=? "
                                   "AND camera_id=? AND event_type='PERSON_RECOGNIZED' "
                                   "ORDER BY event_time DESC LIMIT 1", (person_id, camera_id)).fetchone()
            evidence = json.loads(recognized['metadata_json'] or '{}') if recognized else {}
            if recognized:
                recognized_at=datetime.fromisoformat(recognized['event_time'].replace('Z','+00:00'))
                if recognized_at.tzinfo is None: recognized_at=recognized_at.replace(tzinfo=timezone.utc)
                if not 0 <= (now-recognized_at).total_seconds() <= self.candidate_seconds:
                    evidence={}
                    recognized=None

            if action == "CHECK_IN":
                if session:
                    c.execute(
                        "UPDATE attendance_sessions SET entry_confirmed=1,arrival_time=?,"
                        "arrival_camera=?,arrival_confidence=?,arrival_snapshot=? WHERE id=?",
                        (stamp, camera_id, ev.confidence, action_snapshot, session["id"]),
                    )
                else:
                    c.execute(
                        "INSERT INTO attendance_sessions(id,person_id,store_id,arrival_time,"
                        "arrival_camera,arrival_confidence,arrival_snapshot,status,entry_confirmed) "
                        "VALUES(?,?,?,?,?,?,?,'OPEN',1)",
                        (
                            session_id, person_id, self.engine.store_id, stamp,
                            camera_id, ev.confidence, action_snapshot,
                        ),
                    )
            elif action == "CHECK_OUT":
                if not session:
                    raise ValueError("No open attendance session")
                c.execute(
                    "UPDATE attendance_sessions SET exit_time=?,exit_camera=?,exit_confidence=?,"
                    "exit_snapshot=?,status='CLOSED',break_started_at=NULL WHERE id=?",
                    (stamp, camera_id, ev.confidence, action_snapshot, session["id"]),
                )
            elif action == "START_BREAK":
                if not session:
                    raise ValueError("No open attendance session")
                c.execute(
                    "UPDATE attendance_sessions SET break_started_at=?,last_break_start=?,"
                    "last_break_end=NULL WHERE id=?",
                    (stamp, stamp, session["id"]),
                )
            elif action == "END_BREAK":
                if not session:
                    raise ValueError("No open attendance session")
                c.execute(
                    "UPDATE attendance_sessions SET break_started_at=NULL,last_break_end=? WHERE id=?",
                    (stamp, session["id"]),
                )
            else:
                raise ValueError("Unsupported attendance action")

            metadata = {
                "action": action,
                "source": "local_attendance_station",
                "prior_state": expected_state,
                "candidate_token": token,
                "confidence": ev.confidence,
                "evidence_path": action_snapshot,
                "snapshot_path": action_snapshot,
                "attendance_action_evidence": True,
                "snapshot_paths": list(dict.fromkeys([p for p in [action_snapshot, *(evidence.get('snapshot_paths') or [])] if p]))[:3],
                "clip_path": evidence.get('clip_path'),
                "evidence_missing": {"clip": "recognition_clip_not_available"} if not evidence.get('clip_path') else {},
                "crm_confirmed_break": action in {"START_BREAK", "END_BREAK"},
                **store.attendance_sync_metadata(c, person_id, session_id, "MANUAL"),
                "recognition_event_id": recognized['id'] if recognized else f"{camera_id}:{ev.track_id}:{ev.timestamp.isoformat()}",
            }
            c.execute(
                "INSERT INTO person_events VALUES(?,?,?,?,?,?,?)",
                (
                    "manual:" + request_id, person_id, self.engine.store_id, camera_id,
                    "MANUAL_" + action, stamp, json.dumps(metadata),
                ),
            )
            event_type = {"CHECK_IN": "ATTENDANCE_ENTRY", "CHECK_OUT": "ATTENDANCE_EXIT",
                          "START_BREAK": "BREAK_START", "END_BREAK": "BREAK_END"}[action]
            store._enqueue_edge_event(c, "manual:" + request_id, event_type, {
                "event_id": "manual:" + request_id, "person_id": person_id,
                "store_id": self.engine.store_id, "camera_id": camera_id,
                "event_type": event_type, "event_time": stamp, "metadata": metadata,
            })
            return {"applied": True, "duplicate": False, "state_before": expected_state,
                    "session_id": session_id, "event_id": "manual:" + request_id}
