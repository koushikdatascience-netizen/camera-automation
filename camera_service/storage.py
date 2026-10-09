from __future__ import annotations
import json, sqlite3, threading, uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

class SQLiteStore:
    def __init__(self, path: str):
        self.path=path; Path(path).parent.mkdir(parents=True, exist_ok=True); self._lock=threading.RLock(); self._init()
    @contextmanager
    def _conn(self):
        c=sqlite3.connect(self.path, timeout=30, check_same_thread=False); c.row_factory=sqlite3.Row
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()
    def _init(self):
        with self._conn() as c:
            c.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS personnel(id TEXT PRIMARY KEY, employee_code TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL, role TEXT NOT NULL, phone TEXT, email TEXT, active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS face_profiles(id TEXT PRIMARY KEY, person_id TEXT NOT NULL, embedding_json TEXT NOT NULL, quality REAL NOT NULL, created_at TEXT NOT NULL, FOREIGN KEY(person_id) REFERENCES personnel(id));
            CREATE INDEX IF NOT EXISTS idx_face_person ON face_profiles(person_id);
            CREATE TABLE IF NOT EXISTS attendance_sessions(id TEXT PRIMARY KEY, person_id TEXT NOT NULL, store_id TEXT NOT NULL, arrival_time TEXT NOT NULL, exit_time TEXT, arrival_camera TEXT, exit_camera TEXT, arrival_confidence REAL, exit_confidence REAL, arrival_snapshot TEXT, exit_snapshot TEXT, status TEXT NOT NULL, FOREIGN KEY(person_id) REFERENCES personnel(id));
            CREATE INDEX IF NOT EXISTS idx_attendance_person_status ON attendance_sessions(person_id,store_id,status);
            CREATE TABLE IF NOT EXISTS person_events(id TEXT PRIMARY KEY, person_id TEXT, store_id TEXT, camera_id TEXT, event_type TEXT NOT NULL, event_time TEXT NOT NULL, metadata_json TEXT);
            CREATE TABLE IF NOT EXISTS edge_event_queue(id TEXT PRIMARY KEY, event_type TEXT NOT NULL, payload_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING', attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at TEXT NOT NULL, next_attempt_at TEXT, claimed_at TEXT, synced_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_edge_event_queue_status ON edge_event_queue(status,created_at);
            CREATE TABLE IF NOT EXISTS unknown_incidents(id TEXT PRIMARY KEY, store_id TEXT NOT NULL, camera_id TEXT NOT NULL, track_id TEXT NOT NULL, first_seen TEXT NOT NULL, confirmed_unknown_at TEXT NOT NULL, last_seen TEXT NOT NULL, recognition_attempts INTEGER NOT NULL, best_similarity REAL, best_face_snapshot TEXT, best_person_snapshot TEXT, clip_path TEXT, status TEXT NOT NULL DEFAULT 'OPEN', acknowledged_at TEXT);
            CREATE INDEX IF NOT EXISTS idx_unknown_active ON unknown_incidents(store_id,camera_id,track_id,status);
            CREATE TABLE IF NOT EXISTS security_alerts(id TEXT PRIMARY KEY, store_id TEXT NOT NULL, camera_id TEXT NOT NULL, alert_type TEXT NOT NULL, object_label TEXT NOT NULL, confidence REAL NOT NULL, event_time TEXT NOT NULL, snapshot_path TEXT, clip_path TEXT, status TEXT NOT NULL DEFAULT 'OPEN', acknowledged_at TEXT, metadata_json TEXT);
            CREATE INDEX IF NOT EXISTS idx_security_alerts_status ON security_alerts(status,event_time);
            CREATE TABLE IF NOT EXISTS object_security_events(id TEXT PRIMARY KEY, camera_id TEXT NOT NULL, object_class TEXT NOT NULL, confidence REAL NOT NULL, track_id TEXT, detected_at TEXT NOT NULL, confirmed INTEGER NOT NULL DEFAULT 0, alert_sent INTEGER NOT NULL DEFAULT 0, snapshot_path TEXT, model_version TEXT, metadata_json TEXT);
            CREATE INDEX IF NOT EXISTS idx_object_security_events_time ON object_security_events(detected_at);
            ''')
            self._ensure_column(c,'face_profiles','image_path','TEXT')
            self._ensure_column(c,'attendance_sessions','arrival_snapshot','TEXT')
            self._ensure_column(c,'attendance_sessions','exit_snapshot','TEXT')
            self._ensure_column(c,'personnel','attendance_mode','TEXT')
            self._ensure_column(c,'attendance_sessions','entry_confirmed','INTEGER NOT NULL DEFAULT 0')
            for column in ('break_started_at', 'last_break_start', 'last_break_end'):
                self._ensure_column(c,'attendance_sessions',column,'TEXT')
            self._ensure_column(c,'edge_event_queue','next_attempt_at','TEXT')
            self._ensure_column(c,'edge_event_queue','claimed_at','TEXT')
            # Provenance is required for safe authoritative CRM reconciliation. Existing
            # databases predate this column, so those rows are quarantinable legacy
            # identities rather than being misclassified as intentional local-only users.
            self._ensure_column(c,'personnel','managed_source',"TEXT NOT NULL DEFAULT 'legacy'")
            self._recover_interrupted_attendance_evidence(c)
            self._release_stalled_unknown_incidents(c)
    def _ensure_column(self,conn,table,column,definition):
        existing={row['name'] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    def _recover_interrupted_attendance_evidence(self,conn):
        cutoff=datetime.now(timezone.utc).timestamp()-30
        rows=conn.execute("SELECT id,payload_json,created_at FROM edge_event_queue WHERE status='PENDING'").fetchall()
        for row in rows:
            try:
                created=datetime.fromisoformat(str(row['created_at']).replace('Z','+00:00'))
                metadata=json.loads(row['payload_json']).get('metadata') or {}
                if not metadata.get('evidence_pending') or created.timestamp()>cutoff:
                    continue
                metadata['evidence_pending']=False
                metadata['evidence_status']='PARTIAL' if metadata.get('snapshot_paths') else 'UNAVAILABLE'
                missing=metadata.get('evidence_missing') if isinstance(metadata.get('evidence_missing'),dict) else {}
                missing.setdefault('clip','capture_interrupted_by_edge_restart')
                if len(metadata.get('snapshot_paths') or [])<3:
                    missing.setdefault('snapshots','capture_interrupted_by_edge_restart')
                metadata['evidence_missing']=missing
                payload=json.loads(row['payload_json']);payload['metadata']=metadata
                conn.execute("UPDATE edge_event_queue SET payload_json=? WHERE id=?",(json.dumps(payload),row['id']))
                conn.execute("UPDATE person_events SET metadata_json=? WHERE id=?",(json.dumps(metadata),row['id']))
            except (ValueError,TypeError,json.JSONDecodeError):
                continue
    def _release_stalled_unknown_incidents(self, conn):
        """Unblock legacy unknown alerts queued without any clip producer.

        Unknown incidents must reach the cloud even when only a snapshot exists.
        Preserve existing media references and allow a later clip upload if present.
        """
        rows = conn.execute(
            "SELECT id,payload_json FROM edge_event_queue "
            "WHERE status='PENDING' AND event_type='UNKNOWN_INCIDENT'"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                metadata = payload.get("metadata") or {}
                if not metadata.get("evidence_pending"):
                    continue
                metadata["evidence_pending"] = False
                metadata["evidence_status"] = (
                    "PARTIAL" if metadata.get("person_path") or metadata.get("face_path")
                    else "UNAVAILABLE"
                )
                metadata.setdefault("evidence_missing", {}).setdefault(
                    "clip", "not_captured"
                )
                payload["metadata"] = metadata
                conn.execute(
                    "UPDATE edge_event_queue SET payload_json=? WHERE id=?",
                    (json.dumps(payload), row["id"]),
                )
            except (ValueError, TypeError, AttributeError):
                continue

    @staticmethod
    def now(): return datetime.now(timezone.utc).isoformat()
    def _enqueue_edge_event(self,conn,event_id,event_type,payload):
        payload=self._scope_event_payload({**payload,'schema_version':'local.event.v1'})
        conn.execute("INSERT OR IGNORE INTO edge_event_queue(id,event_type,payload_json,status,attempts,last_error,created_at,next_attempt_at,claimed_at,synced_at) VALUES(?,?,?,'PENDING',0,NULL,?,NULL,NULL,NULL)",(event_id,event_type,json.dumps(payload),self.now()))
    def configure_event_scope(self, tenant_id, company_code, shop_id, edge_id):
        self._event_scope={'tenant_id':str(tenant_id),'company_code':str(company_code) if company_code is not None else None,'shop_id':str(shop_id),'edge_id':str(edge_id)}

    def _scope_event_payload(self,payload):
        scope=getattr(self,'_event_scope',None)
        if not scope:
            return payload
        camera_id=payload.get('camera_id')
        return {**payload,'scope':{**scope,'camera_id':str(camera_id or 'system')}}

    def apply_cloud_personnel(self, items):
        """Mirror the authoritative CRM roster without deleting intentional local-only identities.

        managed_source values:
          crm    - created/confirmed by an authoritative cloud personnel snapshot
          local  - intentionally created on this edge
          legacy - row created before provenance tracking existed

        Legacy rows absent from CRM are deactivated (and therefore excluded from
        recognition) but their biometric/history data is retained for safe migration.
        Confirmed CRM rows absent from a later snapshot are deactivated and their face
        templates are removed so stale CRM identities can never win recognition.
        """
        now=self.now(); seen=set()
        with self._lock,self._conn() as c:
            for item in items:
                pid=str(item["person_id"]); seen.add(pid)
                legacy=c.execute("SELECT id,managed_source FROM personnel WHERE employee_code=? AND id<>?",(str(item["employee_code"]),pid)).fetchone()
                if legacy:
                    old_id=str(legacy["id"])
                    c.execute("UPDATE face_profiles SET person_id=? WHERE person_id=?",(pid,old_id))
                    c.execute("UPDATE attendance_sessions SET person_id=? WHERE person_id=?",(pid,old_id))
                    c.execute("UPDATE person_events SET person_id=? WHERE person_id=?",(pid,old_id))
                    c.execute("DELETE FROM personnel WHERE id=?",(old_id,))
                c.execute("""INSERT INTO personnel(id,employee_code,full_name,role,phone,email,active,created_at,updated_at,managed_source)
                    VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET employee_code=excluded.employee_code,
                    full_name=excluded.full_name,role=excluded.role,phone=excluded.phone,email=excluded.email,
                    active=excluded.active,updated_at=excluded.updated_at,managed_source='crm'""",
                    (pid,str(item["employee_code"]),str(item["full_name"]),str(item["role"]),item.get("phone"),item.get("email"),
                     1 if item.get("active",True) else 0,now,now,'crm'))
                mode=str(item.get('attendance_mode') or '').upper()
                c.execute("UPDATE personnel SET attendance_mode=? WHERE id=?",
                          (mode if mode in {'AUTO','MANUAL'} else 'MANUAL',pid))
                cloud_face_ids=set()
                for face in item.get("faces") or []:
                    fid=str(face["face_id"]); cloud_face_ids.add(fid)
                    c.execute("""INSERT INTO face_profiles(id,person_id,embedding_json,quality,created_at,image_path)
                        VALUES(?,?,?,?,?,NULL) ON CONFLICT(id) DO UPDATE SET person_id=excluded.person_id,
                        embedding_json=excluded.embedding_json,quality=excluded.quality""",
                        (fid,pid,json.dumps(face["embedding"]),float(face.get("quality") or 0),now))
                existing=c.execute("SELECT id FROM face_profiles WHERE person_id=?",(pid,)).fetchall()
                for row in existing:
                    if row["id"] not in cloud_face_ids:
                        c.execute("DELETE FROM face_profiles WHERE id=?",(row["id"],))

            # Only CRM-owned or pre-provenance legacy identities are reconciled.
            # Explicit local-only identities remain untouched.
            stale_rows=c.execute("SELECT id,managed_source FROM personnel WHERE active=1 AND managed_source IN ('crm','legacy')").fetchall()
            stale_ids=[str(row["id"]) for row in stale_rows if str(row["id"]) not in seen]
            removed_faces=0
            for row in stale_rows:
                stale_id=str(row["id"])
                if stale_id in seen:
                    continue
                c.execute("UPDATE personnel SET active=0,updated_at=? WHERE id=?",(now,stale_id))
                if str(row["managed_source"]) == 'crm':
                    removed_faces += c.execute("DELETE FROM face_profiles WHERE person_id=?",(stale_id,)).rowcount
        return {"applied":len(seen),"deactivated":len(stale_ids),"removed_faces":removed_faces,"authoritative":True}

    def create_person(self, d):
        pid=str(uuid.uuid4()); now=self.now()
        with self._lock,self._conn() as c: c.execute("INSERT INTO personnel(id,employee_code,full_name,role,phone,email,active,created_at,updated_at,managed_source) VALUES(?,?,?,?,?,?,?,?,?,?)",(pid,d.employee_code,d.full_name,d.role.value,d.phone,d.email,1,now,now,'local'))
        return self.get_person(pid)
    def list_people(self):
        with self._conn() as c: return [dict(r) for r in c.execute("SELECT * FROM personnel ORDER BY full_name")]
    def get_person(self,pid):
        with self._conn() as c:
            r=c.execute("SELECT * FROM personnel WHERE id=?",(pid,)).fetchone(); return dict(r) if r else None
    def patch_person(self,pid,changes:dict):
        allowed={k:v for k,v in changes.items() if k in {'full_name','role','phone','email','active'} and v is not None}
        if 'role' in allowed and hasattr(allowed['role'],'value'): allowed['role']=allowed['role'].value
        if 'active' in allowed: allowed['active']=int(bool(allowed['active']))
        if not allowed: return self.get_person(pid)
        allowed['updated_at']=self.now(); sql="UPDATE personnel SET "+','.join(f"{k}=?" for k in allowed)+" WHERE id=?"
        with self._lock,self._conn() as c: c.execute(sql,tuple(allowed.values())+(pid,))
        return self.get_person(pid)
    def add_face(self,pid,embedding:list[float],quality:float,image_path=None):
        fid=str(uuid.uuid4())
        with self._lock,self._conn() as c:
            c.execute("INSERT INTO face_profiles(id,person_id,embedding_json,quality,created_at,image_path) VALUES(?,?,?,?,?,?)",(fid,pid,json.dumps(embedding),quality,self.now(),image_path))
        return {'id':fid,'person_id':pid,'quality':quality,'image_path':image_path}
    def list_faces(self,pid):
        with self._conn() as c: return [dict(r) for r in c.execute("SELECT id,person_id,quality,created_at,image_path FROM face_profiles WHERE person_id=? ORDER BY created_at DESC",(pid,))]
    def get_face(self,pid,fid):
        with self._conn() as c:
            r=c.execute("SELECT id,person_id,quality,created_at,image_path FROM face_profiles WHERE person_id=? AND id=?",(pid,fid)).fetchone(); return dict(r) if r else None
    def delete_face(self,pid,fid):
        with self._lock,self._conn() as c:
            cur=c.execute("DELETE FROM face_profiles WHERE id=? AND person_id=?",(fid,pid)); return cur.rowcount>0
    def embeddings(self):
        with self._conn() as c:
            rows=c.execute("SELECT f.id,f.person_id,f.embedding_json,p.full_name,p.role FROM face_profiles f JOIN personnel p ON p.id=f.person_id WHERE p.active=1").fetchall()
            return [{**dict(r),'embedding':json.loads(r['embedding_json'])} for r in rows]
    def open_session(self,person_id,store_id):
        with self._conn() as c:
            r=c.execute("SELECT * FROM attendance_sessions WHERE person_id=? AND store_id=? AND status='OPEN' ORDER BY arrival_time DESC LIMIT 1",(person_id,store_id)).fetchone(); return dict(r) if r else None
    def attendance_sync_metadata(self, conn, person_id, session_id, mode):
        previous = conn.execute("""SELECT id FROM edge_event_queue
            WHERE json_extract(payload_json,'$.person_id')=?
            AND json_extract(payload_json,'$.metadata.attendance_sync_bridge')=1
            ORDER BY rowid DESC LIMIT 1""", (person_id,)).fetchone()
        return {"attendance_sync_bridge": True, "attendance_session_id": session_id,
                "attendance_mode": mode, "attendance_source": "MANUAL" if mode == "MANUAL" else "RECOGNITION",
                "predecessor_event_id": previous["id"] if previous else None}
    def create_arrival(self,person_id,store_id,ts,camera,confidence,snapshot_path=None,confirmed=False):
        with self._lock, self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            row=c.execute("SELECT * FROM attendance_sessions WHERE person_id=? AND store_id=? AND status='OPEN' ORDER BY arrival_time DESC LIMIT 1",(person_id,store_id)).fetchone()
            existing=dict(row) if row else None
            if existing and (not confirmed or existing['entry_confirmed']):
                return existing,False
            sid=existing['id'] if existing else str(uuid.uuid4())
            if existing:
                c.execute("UPDATE attendance_sessions SET arrival_time=?,arrival_camera=?,arrival_confidence=?,arrival_snapshot=?,entry_confirmed=1 WHERE id=?",(ts.isoformat(),camera,confidence,snapshot_path,sid))
            else:
                c.execute("INSERT INTO attendance_sessions(id,person_id,store_id,arrival_time,arrival_camera,arrival_confidence,arrival_snapshot,status,entry_confirmed) VALUES(?,?,?,?,?,?,?, 'OPEN',?)",(sid,person_id,store_id,ts.isoformat(),camera,confidence,snapshot_path,1 if confirmed else 0))
            if confirmed:
                metadata={**self.attendance_sync_metadata(c,person_id,sid,'AUTO'),
                          'confidence':confidence,'snapshot_path':snapshot_path,
                          'attendance_action_evidence':True}
                self._enqueue_edge_event(c,sid,'ATTENDANCE_ENTRY',{'event_id':sid,'person_id':person_id,'store_id':store_id,'camera_id':camera,'event_type':'ATTENDANCE_ENTRY','event_time':ts.isoformat(),'metadata':metadata})
            result=dict(c.execute("SELECT * FROM attendance_sessions WHERE id=?",(sid,)).fetchone())
            return result,not bool(existing)
    def close_exit(self,person_id,store_id,ts,camera,confidence,snapshot_path=None):
        with self._lock:
            s=self.open_session(person_id,store_id)
            if not s: return None,False
            with self._conn() as c:
                c.execute("UPDATE attendance_sessions SET exit_time=?,exit_camera=?,exit_confidence=?,exit_snapshot=?,status='CLOSED' WHERE id=?",(ts.isoformat(),camera,confidence,snapshot_path,s['id']))
                self._enqueue_edge_event(c,f"{s['id']}:exit",'ATTENDANCE_EXIT',{'event_id':f"{s['id']}:exit",'person_id':person_id,'store_id':store_id,'camera_id':camera,'event_type':'ATTENDANCE_EXIT','event_time':ts.isoformat(),'metadata':{'attendance_session_id':s['id'],'confidence':confidence,'snapshot_path':snapshot_path}})
            return self.get_attendance_id(s['id']),True
    def record_attendance_break(self, person_id, store_id, camera_id, event_type, ts, metadata):
        with self._lock, self._conn() as conn:
            conn.execute('BEGIN IMMEDIATE')
            session=conn.execute("SELECT * FROM attendance_sessions WHERE person_id=? AND store_id=? "
                                 "AND status='OPEN' AND entry_confirmed=1 ORDER BY arrival_time DESC LIMIT 1",
                                 (person_id,store_id)).fetchone()
            if not session: raise ValueError('Confirmed attendance session required')
            starting=event_type=='BREAK_START'
            if starting==bool(session['break_started_at']):
                raise ValueError('Break state changed')
            stamp=ts.isoformat()
            if starting:
                conn.execute('UPDATE attendance_sessions SET break_started_at=?,last_break_start=?,last_break_end=NULL WHERE id=?',
                             (stamp,stamp,session['id']))
            else:
                conn.execute('UPDATE attendance_sessions SET break_started_at=NULL,last_break_end=? WHERE id=?',
                             (stamp,session['id']))
            evidence={**metadata,**self.attendance_sync_metadata(conn,person_id,session['id'],'MANUAL'),
                      'crm_confirmed_break':True,'attendance_action_evidence':True}
            event_id=str(uuid.uuid4())
            conn.execute('INSERT INTO person_events VALUES(?,?,?,?,?,?,?)',
                         (event_id,person_id,store_id,camera_id,event_type,stamp,json.dumps(evidence)))
            self._enqueue_edge_event(conn,event_id,event_type,dict(event_id=event_id,person_id=person_id,
                store_id=store_id,camera_id=camera_id,event_type=event_type,event_time=stamp,metadata=evidence))
            return event_id
    def get_attendance_id(self,sid):
        with self._conn() as c: r=c.execute("SELECT * FROM attendance_sessions WHERE id=?",(sid,)).fetchone(); return dict(r) if r else None
    def attendance(self,person_id=None,limit=None,camera_id=None):
        q='''SELECT a.*,p.employee_code,p.full_name,p.role FROM attendance_sessions a JOIN personnel p ON p.id=a.person_id'''; args=[]
        filters=[]
        if person_id: filters.append('a.person_id=?'); args.append(person_id)
        if camera_id: filters.append('(a.arrival_camera=? OR a.exit_camera=?)'); args.extend([camera_id,camera_id])
        if filters: q+=' WHERE '+' AND '.join(filters)
        q+=' ORDER BY a.arrival_time DESC'
        if limit is not None: q+=' LIMIT ?'; args.append(int(limit))
        with self._conn() as c: return [dict(r) for r in c.execute(q,args)]
    def add_person_event(self,person_id,store_id,camera_id,event_type,ts,metadata=None):
        eid=str(uuid.uuid4())
        payload={'event_id':eid,'person_id':person_id,'store_id':store_id,'camera_id':camera_id,'event_type':event_type,'event_time':ts.isoformat(),'metadata':metadata or {}}
        with self._lock,self._conn() as c:
            c.execute("INSERT INTO person_events VALUES(?,?,?,?,?,?,?)",(eid,person_id,store_id,camera_id,event_type,ts.isoformat(),json.dumps(metadata or {})))
            self._enqueue_edge_event(c,eid,event_type,payload)
        return eid

    def update_person_event_evidence(self,event_id,snapshot_paths=None,clip_path=None,
                                     evidence_missing=None,evidence_pending=None):
        """Persist attendance evidence completion to both history and queued sync."""
        with self._lock,self._conn() as c:
            row=c.execute("SELECT metadata_json FROM person_events WHERE id=?",(event_id,)).fetchone()
            if not row:
                return False
            metadata=json.loads(row["metadata_json"] or "{}")
            paths=[str(path) for path in (snapshot_paths or metadata.get("snapshot_paths") or []) if path]
            if paths:
                metadata["snapshot_paths"]=paths[:3]
                metadata["snapshot_path"]=paths[0]
            if clip_path:
                metadata["clip_path"]=str(clip_path)
                (metadata.get("evidence_missing") or {}).pop("clip",None)
            missing=metadata.get("evidence_missing") if isinstance(metadata.get("evidence_missing"),dict) else {}
            missing.update(evidence_missing or {})
            metadata["evidence_missing"]=missing
            if evidence_pending is not None:
                metadata["evidence_pending"]=bool(evidence_pending)
            complete=len(paths)>=3 and bool(metadata.get("clip_path"))
            metadata["evidence_status"]=("PENDING_CAPTURE" if metadata.get("evidence_pending") else
                "COMPLETE" if complete else "PARTIAL" if paths or metadata.get("clip_path") else "UNAVAILABLE")
            c.execute("UPDATE person_events SET metadata_json=? WHERE id=?",(json.dumps(metadata),event_id))
            queued=c.execute("SELECT payload_json,status FROM edge_event_queue WHERE id=?",(event_id,)).fetchone()
            if queued and queued["status"]=="PENDING":
                payload=json.loads(queued["payload_json"])
                payload["metadata"]={**(payload.get("metadata") or {}),**metadata}
                c.execute("UPDATE edge_event_queue SET payload_json=? WHERE id=?",(json.dumps(payload),event_id))
            for linked in c.execute("SELECT id,payload_json FROM edge_event_queue WHERE status='PENDING' "
                                    "AND json_extract(payload_json,'$.metadata.evidence_parent_event_id')=?",(event_id,)).fetchall():
                payload=json.loads(linked['payload_json'])
                payload['metadata'].update({key:metadata[key] for key in
                    ('snapshot_paths','snapshot_path','clip_path','evidence_pending','evidence_status','evidence_missing')
                    if key in metadata})
                c.execute('UPDATE edge_event_queue SET payload_json=? WHERE id=?',(json.dumps(payload),linked['id']))
        return True

    def link_attendance_evidence(self, session_id, recognition_event_id):
        with self._lock,self._conn() as conn:
            queued=conn.execute("SELECT payload_json FROM edge_event_queue WHERE id=? AND status='PENDING'",(session_id,)).fetchone()
            evidence=conn.execute('SELECT metadata_json FROM person_events WHERE id=?',(recognition_event_id,)).fetchone()
            if not queued or not evidence: return
            payload=json.loads(queued['payload_json']); metadata=payload.get('metadata') or {}
            if metadata.get('evidence_parent_event_id'): return
            source=json.loads(evidence['metadata_json'] or '{}')
            metadata['evidence_parent_event_id']=recognition_event_id
            metadata.update({key:source[key] for key in
                ('snapshot_paths','snapshot_path','clip_path','evidence_pending','evidence_status','evidence_missing')
                if key in source})
            payload['metadata']=metadata
            conn.execute('UPDATE edge_event_queue SET payload_json=? WHERE id=?',(json.dumps(payload),session_id))
    def person_events(self,person_id=None):
        q='''SELECT e.*,p.employee_code,p.full_name,p.role FROM person_events e LEFT JOIN personnel p ON p.id=e.person_id'''
        args=[]
        if person_id:
            q+=' WHERE e.person_id=?'; args.append(person_id)
        q+=' ORDER BY e.event_time DESC'
        with self._conn() as c: return [dict(r) for r in c.execute(q,args)]
    def upsert_unknown(self,store_id,camera_id,track_id,first_seen,confirmed,last_seen,attempts,best_similarity,face_path=None,person_path=None,clip_path=None):
        with self._lock:
            with self._conn() as c:
                row=c.execute("SELECT * FROM unknown_incidents WHERE store_id=? AND camera_id=? AND track_id=? AND status='OPEN' LIMIT 1",(store_id,camera_id,track_id)).fetchone()
                if row:
                    c.execute("UPDATE unknown_incidents SET last_seen=?,recognition_attempts=?,best_similarity=COALESCE(?,best_similarity),best_face_snapshot=COALESCE(?,best_face_snapshot),best_person_snapshot=COALESCE(?,best_person_snapshot),clip_path=COALESCE(?,clip_path) WHERE id=?",(last_seen.isoformat(),attempts,best_similarity,face_path,person_path,clip_path,row['id']))
                    return row['id'],False
                iid=str(uuid.uuid4()); c.execute("INSERT INTO unknown_incidents(id,store_id,camera_id,track_id,first_seen,confirmed_unknown_at,last_seen,recognition_attempts,best_similarity,best_face_snapshot,best_person_snapshot,clip_path,status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?, 'OPEN')",(iid,store_id,camera_id,track_id,first_seen.isoformat(),confirmed.isoformat(),last_seen.isoformat(),attempts,best_similarity,face_path,person_path,clip_path))
                payload={'event_id':iid,'store_id':store_id,'camera_id':camera_id,'track_id':track_id,'event_type':'UNKNOWN_INCIDENT','event_time':confirmed.isoformat(),'metadata':{'first_seen':first_seen.isoformat(),'last_seen':last_seen.isoformat(),'attempts':attempts,'best_similarity':best_similarity,'face_path':face_path,'person_path':person_path,'clip_path':clip_path,'evidence_pending':False,'evidence_status':'PARTIAL' if person_path or face_path else 'UNAVAILABLE'}}
                self._enqueue_edge_event(c,iid,'UNKNOWN_INCIDENT',payload)
                return iid,True
    def update_unknown_clip(self,iid,clip_path):
        with self._lock,self._conn() as c:
            c.execute("UPDATE unknown_incidents SET clip_path=? WHERE id=?",(clip_path,iid))
            # The original queued UNKNOWN_INCIDENT payload may have been created before
            # the asynchronous evidence clip finished. Keep pending queue payloads in
            # sync so cloud/CRM delivery receives the final evidence path.
            row=c.execute("SELECT payload_json,status FROM edge_event_queue WHERE id=?",(iid,)).fetchone()
            if row and row["status"]=="PENDING":
                payload=json.loads(row["payload_json"])
                payload.setdefault("metadata",{})["clip_path"]=clip_path
                payload["metadata"]["evidence_pending"]=False
                c.execute("UPDATE edge_event_queue SET payload_json=? WHERE id=?",(json.dumps(payload),iid))
        return self.unknown(iid)

    def unknowns(self,limit=None):
        with self._conn() as c: return [dict(r) for r in c.execute("SELECT * FROM unknown_incidents ORDER BY confirmed_unknown_at DESC LIMIT ?",(limit if limit is not None else -1,))]
    def unknown(self,iid):
        with self._conn() as c: r=c.execute("SELECT * FROM unknown_incidents WHERE id=?",(iid,)).fetchone(); return dict(r) if r else None
    def acknowledge_unknown(self,iid):
        with self._lock,self._conn() as c: c.execute("UPDATE unknown_incidents SET status='ACKNOWLEDGED',acknowledged_at=? WHERE id=?",(self.now(),iid))
        return self.unknown(iid)
    def create_security_alert(self,store_id,camera_id,alert_type,object_label,confidence,event_time,snapshot_path=None,clip_path=None,metadata=None):
        aid=str(uuid.uuid4())
        payload={'event_id':aid,'store_id':store_id,'camera_id':camera_id,'event_type':alert_type,'event_time':event_time.isoformat(),'metadata':{**(metadata or {}),'object_label':object_label,'confidence':confidence,'snapshot_path':snapshot_path,'clip_path':clip_path,'evidence_pending':clip_path is None}}
        with self._lock,self._conn() as c:
            c.execute("INSERT INTO security_alerts(id,store_id,camera_id,alert_type,object_label,confidence,event_time,snapshot_path,clip_path,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",(aid,store_id,camera_id,alert_type,object_label,confidence,event_time.isoformat(),snapshot_path,clip_path,json.dumps(metadata or {})))
            self._enqueue_edge_event(c,aid,alert_type,payload)
        return self.security_alert(aid)
    def security_alerts(self,limit=None):
        with self._conn() as c: return [dict(r) for r in c.execute("SELECT * FROM security_alerts ORDER BY event_time DESC LIMIT ?",(limit if limit is not None else -1,))]
    def security_alert(self,aid):
        with self._conn() as c: r=c.execute("SELECT * FROM security_alerts WHERE id=?",(aid,)).fetchone(); return dict(r) if r else None
    def update_security_alert_clip(self,aid,clip_path):
        with self._lock,self._conn() as c:
            c.execute("UPDATE security_alerts SET clip_path=? WHERE id=?",(clip_path,aid))
            # Clip generation is asynchronous. Update a still-pending queue payload so
            # cloud/CRM sync includes the completed evidence instead of a null clip.
            row=c.execute("SELECT payload_json,status FROM edge_event_queue WHERE id=?",(aid,)).fetchone()
            if row and row["status"]=="PENDING":
                payload=json.loads(row["payload_json"])
                payload.setdefault("metadata",{})["clip_path"]=clip_path
                payload["metadata"]["evidence_pending"]=False
                c.execute("UPDATE edge_event_queue SET payload_json=? WHERE id=?",(json.dumps(payload),aid))
        return self.security_alert(aid)
    def acknowledge_security_alert(self,aid):
        with self._lock,self._conn() as c: c.execute("UPDATE security_alerts SET status='ACKNOWLEDGED',acknowledged_at=? WHERE id=?",(self.now(),aid))
        return self.security_alert(aid)
    def create_object_security_event(self,camera_id,object_class,confidence,detected_at,track_id=None,confirmed=False,alert_sent=False,snapshot_path=None,model_version=None,metadata=None):
        eid=str(uuid.uuid4())
        with self._lock,self._conn() as c:
            c.execute("INSERT INTO object_security_events(id,camera_id,object_class,confidence,track_id,detected_at,confirmed,alert_sent,snapshot_path,model_version,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",(eid,camera_id,object_class,float(confidence),track_id,detected_at.isoformat(),1 if confirmed else 0,1 if alert_sent else 0,snapshot_path,model_version,json.dumps(metadata or {})))
        return self.object_security_event(eid)
    def object_security_events(self):
        with self._conn() as c: return [dict(r) for r in c.execute("SELECT * FROM object_security_events ORDER BY detected_at DESC")]
    def object_security_event(self,eid):
        with self._conn() as c:
            r=c.execute("SELECT * FROM object_security_events WHERE id=?",(eid,)).fetchone(); return dict(r) if r else None
    def queued_events(self,limit=50):
        now=self.now()
        with self._conn() as c:
            rows=c.execute("SELECT * FROM edge_event_queue WHERE status='PENDING' AND (next_attempt_at IS NULL OR next_attempt_at<=?) ORDER BY created_at LIMIT ?",(now,limit)).fetchall()
            return [dict(r) for r in rows]
    def mark_event_synced(self,event_id):
        with self._lock,self._conn() as c:
            c.execute("UPDATE edge_event_queue SET status='SYNCED',synced_at=?,last_error=NULL WHERE id=?",(self.now(),event_id))
    def mark_event_failed(self,event_id,error,retry_after_seconds=0):
        from datetime import timedelta
        next_attempt=(datetime.now(timezone.utc)+timedelta(seconds=max(0,float(retry_after_seconds)))).isoformat()
        with self._lock,self._conn() as c:
            c.execute("UPDATE edge_event_queue SET attempts=attempts+1,last_error=?,next_attempt_at=? WHERE id=?",(str(error)[:1000],next_attempt,event_id))
    def event_queue_status(self):
        with self._conn() as c:
            rows=c.execute("SELECT status,COUNT(*) AS count FROM edge_event_queue GROUP BY status").fetchall()
            counts={r['status']:r['count'] for r in rows}
            failed=c.execute("SELECT id,event_type,attempts,last_error,created_at FROM edge_event_queue WHERE status='PENDING' AND last_error IS NOT NULL ORDER BY created_at DESC LIMIT 5").fetchall()
            return {'counts':counts,'recent_errors':[dict(r) for r in failed]}
