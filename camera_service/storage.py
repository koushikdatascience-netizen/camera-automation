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
            self._ensure_column(c,'attendance_sessions','entry_confirmed','INTEGER NOT NULL DEFAULT 0')
            self._ensure_column(c,'edge_event_queue','next_attempt_at','TEXT')
            self._ensure_column(c,'edge_event_queue','claimed_at','TEXT')
    def _ensure_column(self,conn,table,column,definition):
        existing={row['name'] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    @staticmethod
    def now(): return datetime.now(timezone.utc).isoformat()
    def _enqueue_edge_event(self,conn,event_id,event_type,payload):
        payload={**payload,'schema_version':'local.event.v1'}
        conn.execute("INSERT OR IGNORE INTO edge_event_queue(id,event_type,payload_json,status,attempts,last_error,created_at,next_attempt_at,claimed_at,synced_at) VALUES(?,?,?,'PENDING',0,NULL,?,NULL,NULL,NULL)",(event_id,event_type,json.dumps(payload),self.now()))
    def create_person(self, d):
        pid=str(uuid.uuid4()); now=self.now()
        with self._lock,self._conn() as c: c.execute("INSERT INTO personnel VALUES(?,?,?,?,?,?,?,?,?)",(pid,d.employee_code,d.full_name,d.role.value,d.phone,d.email,1,now,now))
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
    def create_arrival(self,person_id,store_id,ts,camera,confidence,snapshot_path=None,confirmed=False):
        with self._lock:
            existing=self.open_session(person_id,store_id)
            if existing:
                if confirmed and not existing.get('entry_confirmed'):
                    with self._conn() as c:
                        c.execute("UPDATE attendance_sessions SET arrival_time=?,arrival_camera=?,arrival_confidence=?,arrival_snapshot=?,entry_confirmed=1 WHERE id=?",(ts.isoformat(),camera,confidence,snapshot_path,existing['id']))
                        self._enqueue_edge_event(c,existing['id'],'ATTENDANCE_ENTRY',{'event_id':existing['id'],'person_id':person_id,'store_id':store_id,'camera_id':camera,'event_type':'ATTENDANCE_ENTRY','event_time':ts.isoformat(),'metadata':{'attendance_session_id':existing['id'],'confidence':confidence,'snapshot_path':snapshot_path}})
                    return self.open_session(person_id,store_id),False
                return existing,False
            sid=str(uuid.uuid4())
            with self._conn() as c:
                c.execute("INSERT INTO attendance_sessions(id,person_id,store_id,arrival_time,arrival_camera,arrival_confidence,arrival_snapshot,status,entry_confirmed) VALUES(?,?,?,?,?,?,?, 'OPEN',?)",(sid,person_id,store_id,ts.isoformat(),camera,confidence,snapshot_path,1 if confirmed else 0))
                if confirmed:
                    self._enqueue_edge_event(c,sid,'ATTENDANCE_ENTRY',{'event_id':sid,'person_id':person_id,'store_id':store_id,'camera_id':camera,'event_type':'ATTENDANCE_ENTRY','event_time':ts.isoformat(),'metadata':{'attendance_session_id':sid,'confidence':confidence,'snapshot_path':snapshot_path}})
            return self.open_session(person_id,store_id),True
    def close_exit(self,person_id,store_id,ts,camera,confidence,snapshot_path=None):
        with self._lock:
            s=self.open_session(person_id,store_id)
            if not s: return None,False
            with self._conn() as c:
                c.execute("UPDATE attendance_sessions SET exit_time=?,exit_camera=?,exit_confidence=?,exit_snapshot=?,status='CLOSED' WHERE id=?",(ts.isoformat(),camera,confidence,snapshot_path,s['id']))
                self._enqueue_edge_event(c,f"{s['id']}:exit",'ATTENDANCE_EXIT',{'event_id':f"{s['id']}:exit",'person_id':person_id,'store_id':store_id,'camera_id':camera,'event_type':'ATTENDANCE_EXIT','event_time':ts.isoformat(),'metadata':{'attendance_session_id':s['id'],'confidence':confidence,'snapshot_path':snapshot_path}})
            return self.get_attendance_id(s['id']),True
    def get_attendance_id(self,sid):
        with self._conn() as c: r=c.execute("SELECT * FROM attendance_sessions WHERE id=?",(sid,)).fetchone(); return dict(r) if r else None
    def attendance(self,person_id=None):
        q='''SELECT a.*,p.employee_code,p.full_name,p.role FROM attendance_sessions a JOIN personnel p ON p.id=a.person_id'''; args=[]
        if person_id: q+=' WHERE a.person_id=?'; args.append(person_id)
        q+=' ORDER BY a.arrival_time DESC'
        with self._conn() as c: return [dict(r) for r in c.execute(q,args)]
    def add_person_event(self,person_id,store_id,camera_id,event_type,ts,metadata=None):
        eid=str(uuid.uuid4())
        payload={'event_id':eid,'person_id':person_id,'store_id':store_id,'camera_id':camera_id,'event_type':event_type,'event_time':ts.isoformat(),'metadata':metadata or {}}
        with self._lock,self._conn() as c:
            c.execute("INSERT INTO person_events VALUES(?,?,?,?,?,?,?)",(eid,person_id,store_id,camera_id,event_type,ts.isoformat(),json.dumps(metadata or {})))
            self._enqueue_edge_event(c,eid,event_type,payload)
        return eid
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
                payload={'event_id':iid,'store_id':store_id,'camera_id':camera_id,'track_id':track_id,'event_type':'UNKNOWN_INCIDENT','event_time':confirmed.isoformat(),'metadata':{'first_seen':first_seen.isoformat(),'last_seen':last_seen.isoformat(),'attempts':attempts,'best_similarity':best_similarity,'face_path':face_path,'person_path':person_path,'clip_path':clip_path}}
                self._enqueue_edge_event(c,iid,'UNKNOWN_INCIDENT',payload)
                return iid,True
    def unknowns(self):
        with self._conn() as c: return [dict(r) for r in c.execute("SELECT * FROM unknown_incidents ORDER BY confirmed_unknown_at DESC")]
    def unknown(self,iid):
        with self._conn() as c: r=c.execute("SELECT * FROM unknown_incidents WHERE id=?",(iid,)).fetchone(); return dict(r) if r else None
    def acknowledge_unknown(self,iid):
        with self._lock,self._conn() as c: c.execute("UPDATE unknown_incidents SET status='ACKNOWLEDGED',acknowledged_at=? WHERE id=?",(self.now(),iid))
        return self.unknown(iid)
    def create_security_alert(self,store_id,camera_id,alert_type,object_label,confidence,event_time,snapshot_path=None,clip_path=None,metadata=None):
        aid=str(uuid.uuid4())
        payload={'event_id':aid,'store_id':store_id,'camera_id':camera_id,'event_type':alert_type,'event_time':event_time.isoformat(),'metadata':{**(metadata or {}),'object_label':object_label,'confidence':confidence,'snapshot_path':snapshot_path,'clip_path':clip_path}}
        with self._lock,self._conn() as c:
            c.execute("INSERT INTO security_alerts(id,store_id,camera_id,alert_type,object_label,confidence,event_time,snapshot_path,clip_path,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?)",(aid,store_id,camera_id,alert_type,object_label,confidence,event_time.isoformat(),snapshot_path,clip_path,json.dumps(metadata or {})))
            self._enqueue_edge_event(c,aid,alert_type,payload)
        return self.security_alert(aid)
    def security_alerts(self):
        with self._conn() as c: return [dict(r) for r in c.execute("SELECT * FROM security_alerts ORDER BY event_time DESC")]
    def security_alert(self,aid):
        with self._conn() as c: r=c.execute("SELECT * FROM security_alerts WHERE id=?",(aid,)).fetchone(); return dict(r) if r else None
    def update_security_alert_clip(self,aid,clip_path):
        with self._lock,self._conn() as c: c.execute("UPDATE security_alerts SET clip_path=? WHERE id=?",(clip_path,aid))
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
