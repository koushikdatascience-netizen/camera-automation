from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from cloud_portal.attendance_delivery import AttendanceDeliveryStore


class PortalStore(AttendanceDeliveryStore):
    def __init__(self, path: str = "data/cloud_portal.db"):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()
        self.initialize_attendance_delivery()

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY, name TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sites(id TEXT NOT NULL, tenant_id TEXT NOT NULL, name TEXT, created_at TEXT NOT NULL, PRIMARY KEY(id, tenant_id));
                CREATE TABLE IF NOT EXISTS edge_machines(id TEXT NOT NULL, tenant_id TEXT NOT NULL, site_id TEXT NOT NULL, last_seen_at TEXT NOT NULL, PRIMARY KEY(id, tenant_id, site_id));
                CREATE TABLE IF NOT EXISTS edge_heartbeats(tenant_id TEXT NOT NULL, site_id TEXT NOT NULL, edge_id TEXT NOT NULL, received_at TEXT NOT NULL, status_json TEXT NOT NULL, PRIMARY KEY(tenant_id,site_id,edge_id));
                CREATE TABLE IF NOT EXISTS edge_events(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, company_code TEXT, shop_id TEXT, site_id TEXT NOT NULL, edge_id TEXT NOT NULL, store_id TEXT, camera_id TEXT, event_type TEXT NOT NULL, event_time TEXT NOT NULL, received_at TEXT NOT NULL, payload_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS edge_activation_codes(code_hash TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,company_code TEXT,shop_id TEXT NOT NULL,created_by TEXT,created_at TEXT NOT NULL,expires_at TEXT NOT NULL,consumed_at TEXT,consumed_machine_code TEXT);
                CREATE TABLE IF NOT EXISTS camera_configs(tenant_id TEXT NOT NULL, company_code TEXT, shop_id TEXT NOT NULL, site_id TEXT NOT NULL, edge_id TEXT NOT NULL, camera_id TEXT NOT NULL, name TEXT NOT NULL, source_type TEXT NOT NULL, source TEXT NOT NULL, camera_role TEXT NOT NULL, camera_zone TEXT, crowd_threshold INTEGER NOT NULL DEFAULT 10, enabled INTEGER NOT NULL DEFAULT 1, features_json TEXT NOT NULL, settings_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(tenant_id,shop_id,edge_id,camera_id));
                CREATE TABLE IF NOT EXISTS camera_detection_configs(tenant_id TEXT NOT NULL,shop_id TEXT NOT NULL,edge_id TEXT NOT NULL,camera_id TEXT NOT NULL,mode TEXT NOT NULL,zones_json TEXT NOT NULL DEFAULT '[]',version INTEGER NOT NULL DEFAULT 1,sync_status TEXT NOT NULL DEFAULT 'PENDING',applied_version INTEGER NOT NULL DEFAULT 0,local_override INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL,PRIMARY KEY(tenant_id,shop_id,edge_id,camera_id));
                CREATE TABLE IF NOT EXISTS edge_commands(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,shop_id TEXT NOT NULL,edge_id TEXT NOT NULL,command_type TEXT NOT NULL,request_json TEXT NOT NULL,status TEXT NOT NULL, result_json TEXT,created_at TEXT NOT NULL,claimed_at TEXT,completed_at TEXT);
                CREATE TABLE IF NOT EXISTS portal_sessions(session_id TEXT PRIMARY KEY,token_hash TEXT UNIQUE NOT NULL,tenant_id TEXT NOT NULL,company_code TEXT,shop_id TEXT NOT NULL,user_id TEXT,display_name TEXT,role TEXT NOT NULL,created_at TEXT NOT NULL,expires_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS portal_users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,display_name TEXT NOT NULL,tenant_id TEXT NOT NULL,company_code TEXT,shop_id TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'OWNER',enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS cloud_personnel(id TEXT PRIMARY KEY,tenant_id TEXT NOT NULL,shop_id TEXT NOT NULL,employee_code TEXT NOT NULL,full_name TEXT NOT NULL,role TEXT NOT NULL,phone TEXT,email TEXT,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(tenant_id,shop_id,employee_code));
                CREATE TABLE IF NOT EXISTS cloud_face_profiles(id TEXT PRIMARY KEY,person_id TEXT NOT NULL,tenant_id TEXT NOT NULL,shop_id TEXT NOT NULL,embedding_json TEXT NOT NULL,quality REAL NOT NULL,image_path TEXT,created_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_cloud_personnel_scope ON cloud_personnel(tenant_id,shop_id,active);
                CREATE TABLE IF NOT EXISTS crm_enrollment_rejections(
                    tenant_id TEXT NOT NULL,shop_id TEXT NOT NULL,crm_user_id TEXT NOT NULL,
                    cache_key TEXT NOT NULL,reason TEXT NOT NULL,created_at TEXT NOT NULL,
                    PRIMARY KEY(tenant_id,shop_id,crm_user_id,cache_key));
                CREATE INDEX IF NOT EXISTS idx_cloud_faces_person ON cloud_face_profiles(tenant_id,shop_id,person_id);
                CREATE TABLE IF NOT EXISTS crm_person_mappings(tenant_id TEXT NOT NULL,shop_id TEXT NOT NULL,local_person_id TEXT NOT NULL,crm_user_id TEXT NOT NULL,employee_code TEXT,break_master_id TEXT,enabled INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,PRIMARY KEY(tenant_id,shop_id,local_person_id));
                CREATE INDEX IF NOT EXISTS idx_edge_events_tenant_time ON edge_events(tenant_id, site_id, event_time);
                CREATE INDEX IF NOT EXISTS idx_edge_events_type ON edge_events(tenant_id, event_type, event_time);
                """
            )
            self._ensure_column(conn, "edge_events", "company_code", "TEXT")
            self._ensure_column(conn, "edge_events", "shop_id", "TEXT")
            self._ensure_column(conn, "edge_machines", "company_code", "TEXT")
            self._ensure_column(conn, "edge_machines", "shop_id", "TEXT")
            self._ensure_column(conn, "edge_heartbeats", "company_code", "TEXT")
            self._ensure_column(conn, "edge_heartbeats", "shop_id", "TEXT")

    @staticmethod
    def _ensure_column(conn, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def create_edge_activation_code(self, item: dict[str, Any]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("""INSERT INTO edge_activation_codes(code_hash,tenant_id,company_code,shop_id,created_by,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?)""", (item["code_hash"], item["tenant_id"], item.get("company_code"),
                item["shop_id"], item.get("created_by"), item["created_at"], item["expires_at"]))

    def consume_edge_activation_code(self, code_hash: str, machine_code: str) -> dict[str, Any] | None:
        now = self.now()
        with self._lock, self._conn() as conn:
            row = conn.execute("""SELECT tenant_id,company_code,shop_id,expires_at FROM edge_activation_codes
                WHERE code_hash=? AND consumed_at IS NULL AND expires_at>?""", (code_hash, now)).fetchone()
            if not row:
                return None
            conn.execute("UPDATE edge_activation_codes SET consumed_at=?,consumed_machine_code=? WHERE code_hash=?",
                         (now, machine_code, code_hash))
            return dict(row)

    def resolve_edge_credential(self, token_hash: str):
        return None

    def provision_edge_credential(self, token_hash: str, tenant_id: str, company_code: str | None,
                                  shop_id: str, site_id: str, edge_id: str) -> None:
        raise RuntimeError("Scoped edge credentials require PostgreSQL")

    def revoke_edge_credentials(self, tenant_id: str, shop_id: str, edge_id: str) -> int:
        return 0

    def ingest_event(self, envelope: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(envelope["tenant_id"])
        site_id = str(envelope["site_id"])
        edge_id = str(envelope["edge_id"])
        event_id = str(envelope["event_id"])
        now = self.now()
        with self._lock, self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO tenants(id,name,created_at) VALUES(?,?,?)", (tenant_id, tenant_id, now))
            conn.execute("INSERT OR IGNORE INTO sites(id,tenant_id,name,created_at) VALUES(?,?,?,?)", (site_id, tenant_id, site_id, now))
            conn.execute("INSERT OR REPLACE INTO edge_machines(id,tenant_id,site_id,last_seen_at) VALUES(?,?,?,?)", (edge_id, tenant_id, site_id, now))
            inserted = conn.execute(
                "INSERT OR IGNORE INTO edge_events(id,tenant_id,company_code,shop_id,site_id,edge_id,store_id,camera_id,event_type,event_time,received_at,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    event_id,
                    tenant_id,
                    envelope.get("company_code"),
                    envelope.get("shop_id") or site_id,
                    site_id,
                    edge_id,
                    envelope.get("store_id"),
                    envelope.get("camera_id"),
                    envelope.get("event_type"),
                    envelope.get("event_time"),
                    now,
                    json.dumps(envelope),
                ),
            ).rowcount > 0
        return {"ok": True, "event_id": event_id, "tenant_id": tenant_id, "site_id": site_id, "inserted": bool(inserted)}

    def record_portal_event(self, item: dict[str, Any]) -> dict[str, Any]:
        envelope=dict(item); envelope.setdefault("store_id",item.get("shop_id")); envelope.setdefault("payload",{})
        return self.ingest_event(envelope)

    def list_events(self, tenant_id: str, site_id: str | None = None, event_type: str | None = None, limit: int = 100, shop_id: str | None = None, alerts_only: bool = False) -> list[dict[str, Any]]:
        query = "SELECT * FROM edge_events WHERE tenant_id=?"
        args: list[Any] = [tenant_id]
        if shop_id:
            query += " AND shop_id=?"
            args.append(shop_id)
        if site_id:
            query += " AND site_id=?"
            args.append(site_id)
        if event_type:
            query += " AND event_type=?"
            args.append(event_type)
        if alerts_only:
            query += " AND (event_type LIKE '%UNKNOWN%' OR event_type LIKE '%ALERT%' OR event_type LIKE '%INCIDENT%' OR event_type LIKE '%SHOPLIFTING%')"
        query += " ORDER BY event_time DESC LIMIT ?"
        args.append(max(1, min(500, int(limit))))
        with self._conn() as conn:
            rows = conn.execute(query, args).fetchall()
            return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

    def get_event(self, tenant_id: str, shop_id: str, event_id: str):
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM edge_events WHERE tenant_id=? AND shop_id=? AND id=?", (tenant_id, shop_id, event_id)).fetchone()
            return dict(row) | {"payload": json.loads(row["payload_json"])} if row else None

    def tenant_summary(self, tenant_id: str, shop_id: str | None = None) -> dict[str, Any]:
        with self._conn() as conn:
            if shop_id:
                edges = conn.execute("SELECT COUNT(*) AS count FROM edge_machines WHERE tenant_id=? AND shop_id=?", (tenant_id, shop_id)).fetchone()["count"]
                sites = conn.execute("SELECT COUNT(DISTINCT site_id) AS count FROM edge_machines WHERE tenant_id=? AND shop_id=?", (tenant_id, shop_id)).fetchone()["count"]
                events = conn.execute("SELECT event_type, COUNT(*) AS count FROM edge_events WHERE tenant_id=? AND shop_id=? GROUP BY event_type", (tenant_id, shop_id)).fetchall()
            else:
                sites = conn.execute("SELECT COUNT(*) AS count FROM sites WHERE tenant_id=?", (tenant_id,)).fetchone()["count"]
                edges = conn.execute("SELECT COUNT(*) AS count FROM edge_machines WHERE tenant_id=?", (tenant_id,)).fetchone()["count"]
                events = conn.execute("SELECT event_type, COUNT(*) AS count FROM edge_events WHERE tenant_id=? GROUP BY event_type", (tenant_id,)).fetchall()
            return {"tenant_id": tenant_id, "sites": sites, "edges": edges, "events": {row["event_type"]: row["count"] for row in events}}


    def create_cloud_person(self,item):
        now=self.now()
        with self._lock,self._conn() as c:
            c.execute("INSERT INTO cloud_personnel(id,tenant_id,shop_id,employee_code,full_name,role,phone,email,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
                (item["id"],item["tenant_id"],item["shop_id"],item["employee_code"],item["full_name"],item["role"],item.get("phone"),item.get("email"),now,now))
        return self.get_cloud_person(item["tenant_id"],item["shop_id"],item["id"])
    def upsert_crm_cloud_person(self,item):
        with self._lock,self._conn() as conn:
            owner=conn.execute("SELECT tenant_id,shop_id FROM cloud_personnel WHERE id=?",(item['id'],)).fetchone()
            if owner and (owner['tenant_id']!=item['tenant_id'] or owner['shop_id']!=item['shop_id']):
                raise ValueError('person identity belongs to another scope')
            if owner:
                conn.execute("UPDATE cloud_personnel SET employee_code=?,full_name=?,role=?,phone=?,email=?,active=?,updated_at=? WHERE id=?",
                    (item['employee_code'],item['full_name'],item['role'],item.get('phone'),item.get('email'),
                     int(bool(item.get('active',True))),self.now(),item['id']))
            else:
                conn.execute("INSERT INTO cloud_personnel(id,tenant_id,shop_id,employee_code,full_name,role,phone,email,active,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (item['id'],item['tenant_id'],item['shop_id'],item['employee_code'],item['full_name'],item['role'],
                     item.get('phone'),item.get('email'),int(bool(item.get('active',True))),self.now(),self.now()))
        return self.get_cloud_person(item['tenant_id'],item['shop_id'],item['id'])
    def list_cloud_people(self,tenant_id,shop_id):
        with self._conn() as c:
            rows=c.execute("""SELECT p.*,COUNT(f.id) face_count FROM cloud_personnel p LEFT JOIN cloud_face_profiles f ON f.person_id=p.id
                WHERE p.tenant_id=? AND p.shop_id=? GROUP BY p.id ORDER BY p.full_name""",(tenant_id,shop_id)).fetchall()
        return [dict(r) for r in rows]
    def get_cloud_person(self,tenant_id,shop_id,person_id):
        with self._conn() as c:
            r=c.execute("SELECT * FROM cloud_personnel WHERE tenant_id=? AND shop_id=? AND id=?",(tenant_id,shop_id,person_id)).fetchone()
        return dict(r) if r else None
    def update_cloud_person(self,tenant_id,shop_id,person_id,changes):
        allowed={k:v for k,v in changes.items() if k in {"full_name","role","phone","email","active"} and v is not None}
        if "active" in allowed: allowed["active"]=int(bool(allowed["active"]))
        if allowed:
            allowed["updated_at"]=self.now(); sql="UPDATE cloud_personnel SET "+",".join(f"{k}=?" for k in allowed)+" WHERE tenant_id=? AND shop_id=? AND id=?"
            with self._lock,self._conn() as c: c.execute(sql,tuple(allowed.values())+(tenant_id,shop_id,person_id))
        return self.get_cloud_person(tenant_id,shop_id,person_id)
    def crm_enrollment_rejected(self,tenant,shop,user,key):
        with self._conn() as conn:
            return conn.execute("SELECT 1 FROM crm_enrollment_rejections WHERE tenant_id=? AND shop_id=? AND crm_user_id=? AND cache_key=?",
                (tenant,shop,user,key)).fetchone() is not None
    def reject_crm_enrollment(self,tenant,shop,user,key,reason):
        with self._lock,self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO crm_enrollment_rejections VALUES(?,?,?,?,?,?)",(tenant,shop,user,key,reason,self.now()))

    def add_cloud_face(self,item):
        with self._lock,self._conn() as c:
            c.execute("INSERT INTO cloud_face_profiles(id,person_id,tenant_id,shop_id,embedding_json,quality,image_path,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (item["id"],item["person_id"],item["tenant_id"],item["shop_id"],json.dumps(item["embedding"]),item["quality"],item.get("image_path"),self.now()))
        return {"id":item["id"],"person_id":item["person_id"],"quality":item["quality"],"image_path":item.get("image_path")}
    def list_cloud_faces(self,tenant_id,shop_id,person_id,include_embedding=False):
        cols="id,person_id,quality,image_path,created_at"+(",embedding_json" if include_embedding else "")
        with self._conn() as c: rows=c.execute(f"SELECT {cols} FROM cloud_face_profiles WHERE tenant_id=? AND shop_id=? AND person_id=? ORDER BY created_at DESC",(tenant_id,shop_id,person_id)).fetchall()
        out=[]
        for r in rows:
            item=dict(r)
            if include_embedding: item["embedding"]=json.loads(item.pop("embedding_json"))
            out.append(item)
        return out
    def delete_cloud_face(self,tenant_id,shop_id,person_id,face_id):
        with self._lock,self._conn() as c: return c.execute("DELETE FROM cloud_face_profiles WHERE tenant_id=? AND shop_id=? AND person_id=? AND id=?",(tenant_id,shop_id,person_id,face_id)).rowcount>0

    def record_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(payload["tenant_id"])
        site_id = str(payload["site_id"])
        edge_id = str(payload["edge_id"])
        company_code = payload.get("company_code")
        shop_id = str(payload.get("shop_id") or site_id)
        now = self.now()
        with self._lock, self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO tenants(id,name,created_at) VALUES(?,?,?)", (tenant_id, tenant_id, now))
            conn.execute("INSERT OR IGNORE INTO sites(id,tenant_id,name,created_at) VALUES(?,?,?,?)", (site_id, tenant_id, site_id, now))
            conn.execute("""INSERT INTO edge_machines(id,tenant_id,site_id,last_seen_at,company_code,shop_id)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(id,tenant_id,site_id) DO UPDATE SET
                last_seen_at=excluded.last_seen_at,company_code=excluded.company_code,shop_id=excluded.shop_id""",
                (edge_id, tenant_id, site_id, now, company_code, shop_id))
            conn.execute("""INSERT INTO edge_heartbeats(tenant_id,site_id,edge_id,received_at,status_json,company_code,shop_id)
                VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,site_id,edge_id) DO UPDATE SET
                received_at=excluded.received_at,status_json=excluded.status_json,
                company_code=excluded.company_code,shop_id=excluded.shop_id""",
                (tenant_id, site_id, edge_id, now, json.dumps(payload.get("status") or {}), company_code, shop_id))
        return {"ok": True, "tenant_id": tenant_id, "site_id": site_id, "edge_id": edge_id, "received_at": now}

    def list_edges(self, tenant_id: str, shop_id: str | None = None) -> list[dict[str, Any]]:
        query = """SELECT m.id AS edge_id,m.tenant_id,m.company_code,m.shop_id,m.site_id,m.last_seen_at,
                          h.received_at,h.status_json
                   FROM edge_machines m
                   LEFT JOIN edge_heartbeats h ON h.tenant_id=m.tenant_id AND h.site_id=m.site_id AND h.edge_id=m.id
                   WHERE m.tenant_id=?"""
        args: list[Any] = [tenant_id]
        if shop_id:
            query += " AND m.shop_id=?"
            args.append(shop_id)
        query += " ORDER BY m.last_seen_at DESC"
        with self._conn() as conn:
            rows = conn.execute(query, args).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["status"] = json.loads(data.pop("status_json") or "{}")
            result.append(data)
        return result


    def upsert_camera(self, camera: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        values = (
            camera["tenant_id"], camera.get("company_code"), camera["shop_id"], camera["site_id"],
            camera["edge_id"], camera["camera_id"], camera["name"], camera.get("source_type", "rtsp"),
            camera["source"], camera.get("camera_role", "GENERAL"), camera.get("camera_zone"),
            int(camera.get("crowd_threshold", 10)), 1 if camera.get("enabled", True) else 0,
            json.dumps(camera.get("features") or {}), json.dumps(camera.get("settings") or {}), now, now,
        )
        with self._lock, self._conn() as conn:
            conn.execute("""INSERT INTO camera_configs(
                tenant_id,company_code,shop_id,site_id,edge_id,camera_id,name,source_type,source,camera_role,
                camera_zone,crowd_threshold,enabled,features_json,settings_json,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(tenant_id,shop_id,edge_id,camera_id) DO UPDATE SET
                company_code=excluded.company_code,site_id=excluded.site_id,name=excluded.name,
                source_type=excluded.source_type,source=excluded.source,camera_role=excluded.camera_role,
                camera_zone=excluded.camera_zone,crowd_threshold=excluded.crowd_threshold,enabled=excluded.enabled,
                features_json=excluded.features_json,settings_json=excluded.settings_json,updated_at=excluded.updated_at""", values)
        return self.get_camera(camera["tenant_id"], camera["shop_id"], camera["edge_id"], camera["camera_id"])

    def get_camera(self, tenant_id: str, shop_id: str, edge_id: str, camera_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("""SELECT * FROM camera_configs WHERE tenant_id=? AND shop_id=? AND edge_id=? AND camera_id=?""",
                               (tenant_id, shop_id, edge_id, camera_id)).fetchone()
        return self._camera_row(row) if row else None

    def list_cameras(self, tenant_id: str, shop_id: str | None = None, edge_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM camera_configs WHERE tenant_id=?"
        args: list[Any] = [tenant_id]
        if shop_id:
            query += " AND shop_id=?"; args.append(shop_id)
        if edge_id:
            query += " AND edge_id=?"; args.append(edge_id)
        query += " ORDER BY name,camera_id"
        with self._conn() as conn:
            rows = conn.execute(query, args).fetchall()
        result=[self._camera_row(row) for row in rows]
        for camera in result: camera["detection_config"]=self.get_detection_config(tenant_id,camera["shop_id"],camera["edge_id"],camera["camera_id"])
        return result

    def get_detection_config(self,tenant_id:str,shop_id:str,edge_id:str,camera_id:str)->dict[str,Any]:
        with self._conn() as conn:
            row=conn.execute("SELECT * FROM camera_detection_configs WHERE tenant_id=? AND shop_id=? AND edge_id=? AND camera_id=?",(tenant_id,shop_id,edge_id,camera_id)).fetchone()
        if not row:return {"camera_id":camera_id,"mode":"FULL_FRAME","zones":[],"version":0,"applied_version":0,"sync_status":"DEFAULT","local_override":False,"effective_mode":"FULL_FRAME","effective_zones":[{"id":"FULL_FRAME","name":"Full frame","x":0,"y":0,"width":1,"height":1,"enabled":True}]}
        zones=json.loads(row["zones_json"] or "[]");mode=row["mode"]
        return {"camera_id":camera_id,"mode":mode,"zones":zones,"version":int(row["version"]),"applied_version":int(row["applied_version"]),"sync_status":row["sync_status"],"local_override":bool(row["local_override"]),"effective_mode":mode if mode=="FULL_FRAME" or any(z.get("enabled") for z in zones) else "DISABLED","effective_zones":[{"id":"FULL_FRAME","name":"Full frame","x":0,"y":0,"width":1,"height":1,"enabled":True}] if mode=="FULL_FRAME" else [z for z in zones if z.get("enabled")]}

    def replace_detection_config(self,tenant_id:str,shop_id:str,edge_id:str,camera_id:str,mode:str,zones:list[dict[str,Any]],expected_version:int)->dict[str,Any]:
        mode=str(mode or "").upper()
        if mode not in {"FULL_FRAME","CUSTOM_ZONES"}:raise ValueError("mode must be FULL_FRAME or CUSTOM_ZONES")
        if mode=="FULL_FRAME" and zones:raise ValueError("FULL_FRAME mode does not accept custom zones")
        if len(zones)>64:raise ValueError("At most 64 custom zones are allowed")
        clean=[];ids=set()
        for zone in zones:
            zid=str(zone.get("id") or "").strip();name=str(zone.get("name") or "Detection Zone").strip()
            try:x,y,w,h=(float(zone[key]) for key in ("x","y","width","height"))
            except (KeyError,TypeError,ValueError):raise ValueError("Zone coordinates are required")
            if not zid or len(zid)>128 or zid in ids:raise ValueError("Zone IDs must be unique and 1 to 128 characters")
            if not name or len(name)>80:raise ValueError("Zone name must contain 1 to 80 characters")
            if not(0<=x<=1 and 0<=y<=1 and 0<w<=1 and 0<h<=1 and x+w<=1.000001 and y+h<=1.000001):raise ValueError("Zone coordinates must be normalized and fit inside the camera frame")
            ids.add(zid);clean.append({"id":zid,"name":name,"x":x,"y":y,"width":w,"height":h,"enabled":bool(zone.get("enabled",True))})
        zones=sorted(clean,key=lambda item:item["id"])
        with self._lock,self._conn() as conn:
            if not conn.execute("SELECT 1 FROM camera_configs WHERE tenant_id=? AND shop_id=? AND edge_id=? AND camera_id=?",(tenant_id,shop_id,edge_id,camera_id)).fetchone():raise LookupError("Camera not found")
            conn.execute("INSERT OR IGNORE INTO camera_detection_configs(tenant_id,shop_id,edge_id,camera_id,mode,zones_json,version,sync_status,applied_version,local_override,updated_at) VALUES(?,?,?,?,'FULL_FRAME','[]',0,'DEFAULT',0,0,?)",(tenant_id,shop_id,edge_id,camera_id,self.now()))
            old=conn.execute("SELECT * FROM camera_detection_configs WHERE tenant_id=? AND shop_id=? AND edge_id=? AND camera_id=?",(tenant_id,shop_id,edge_id,camera_id)).fetchone()
            version=int(old["version"]) if old else 0
            if version!=expected_version:raise RuntimeError(f"Detection configuration version conflict: expected {expected_version}, current {version}")
            oldzones=json.loads(old["zones_json"] or "[]") if old else []
            if old and old["mode"]==mode and oldzones==zones:return self.get_detection_config(tenant_id,shop_id,edge_id,camera_id)
            conn.execute("""INSERT INTO camera_detection_configs(tenant_id,shop_id,edge_id,camera_id,mode,zones_json,version,sync_status,applied_version,local_override,updated_at)
                VALUES(?,?,?,?,?,?,?,'PENDING',0,0,?) ON CONFLICT(tenant_id,shop_id,edge_id,camera_id) DO UPDATE SET mode=excluded.mode,zones_json=excluded.zones_json,version=excluded.version,sync_status='PENDING',local_override=0,updated_at=excluded.updated_at""",
                (tenant_id,shop_id,edge_id,camera_id,mode,json.dumps(zones),version+1,self.now()))
        return self.get_detection_config(tenant_id,shop_id,edge_id,camera_id)

    def acknowledge_detection_config(self,tenant_id:str,shop_id:str,edge_id:str,camera_id:str,version:int,status:str,local_override:bool=False)->bool:
        with self._conn() as conn:
            result=conn.execute("UPDATE camera_detection_configs SET applied_version=CASE WHEN ?='APPLIED' THEN ? ELSE applied_version END,sync_status=?,local_override=? WHERE tenant_id=? AND shop_id=? AND edge_id=? AND camera_id=? AND version=?",(status,version,status,int(local_override),tenant_id,shop_id,edge_id,camera_id,version))
            if not result.rowcount and version==0:
                exists=conn.execute("SELECT 1 FROM camera_configs WHERE tenant_id=? AND shop_id=? AND edge_id=? AND camera_id=?",(tenant_id,shop_id,edge_id,camera_id)).fetchone()
                if exists:
                    conn.execute("INSERT OR IGNORE INTO camera_detection_configs(tenant_id,shop_id,edge_id,camera_id,mode,zones_json,version,sync_status,applied_version,local_override,updated_at) VALUES(?,?,?,?,'FULL_FRAME','[]',0,?,0,0,?)",(tenant_id,shop_id,edge_id,camera_id,status,self.now()));return True
        return bool(result.rowcount)

    def delete_camera(self, tenant_id: str, shop_id: str, edge_id: str, camera_id: str) -> bool:
        with self._lock, self._conn() as conn:
            result = conn.execute("""DELETE FROM camera_configs WHERE tenant_id=? AND shop_id=? AND edge_id=? AND camera_id=?""",
                                  (tenant_id, shop_id, edge_id, camera_id))
        return bool(result.rowcount)

    @staticmethod
    def _camera_row(row) -> dict[str, Any]:
        data = dict(row)
        data["enabled"] = bool(data["enabled"])
        data["features"] = json.loads(data.pop("features_json") or "{}")
        data["settings"] = json.loads(data.pop("settings_json") or "{}")
        return data


    def create_edge_command(self, command: dict[str, Any]) -> dict[str, Any]:
        import uuid
        command_id=str(uuid.uuid4()); now=self.now()
        with self._lock,self._conn() as conn:
            conn.execute("INSERT INTO edge_commands(id,tenant_id,shop_id,edge_id,command_type,request_json,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (command_id,command["tenant_id"],command["shop_id"],command["edge_id"],command["command_type"],json.dumps(command.get("request") or {}),"PENDING",now))
        return {"id":command_id,"status":"PENDING"}

    def claim_edge_commands(self, tenant_id: str, shop_id: str, edge_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._lock,self._conn() as conn:
            from datetime import timedelta
            cutoff=(datetime.now(timezone.utc)-timedelta(minutes=2)).isoformat()
            conn.execute("UPDATE edge_commands SET status='PENDING' WHERE tenant_id=? AND shop_id=? AND edge_id=? AND status='CLAIMED' AND claimed_at<?", (tenant_id,shop_id,edge_id,cutoff))
            rows=conn.execute("""SELECT * FROM edge_commands WHERE tenant_id=? AND shop_id=? AND edge_id=? AND status='PENDING'
                ORDER BY created_at LIMIT ?""",(tenant_id,shop_id,edge_id,max(1,min(20,int(limit))))).fetchall()
            result=[]
            for row in rows:
                conn.execute("UPDATE edge_commands SET status='CLAIMED',claimed_at=? WHERE id=? AND status='PENDING'",(self.now(),row["id"]))
                result.append({"id":row["id"],"command_type":row["command_type"],"request":json.loads(row["request_json"])})
            return result

    def complete_edge_command(self, command_id: str, tenant_id: str, shop_id: str, edge_id: str, status: str, result: dict[str, Any]) -> bool:
        with self._lock,self._conn() as conn:
            out=conn.execute("""UPDATE edge_commands SET status=?,result_json=?,completed_at=? WHERE id=? AND tenant_id=? AND shop_id=? AND edge_id=? AND status='CLAIMED'""",
                (status,json.dumps(result),self.now(),command_id,tenant_id,shop_id,edge_id))
        return bool(out.rowcount)

    def get_edge_command(self, command_id: str, tenant_id: str, shop_id: str | None = None) -> dict[str, Any] | None:
        clauses=["id=?","tenant_id=?"]
        params=[command_id, tenant_id]
        if shop_id:
            clauses.append("shop_id=?")
            params.append(shop_id)
        with self._conn() as conn:
            row=conn.execute("SELECT * FROM edge_commands WHERE "+" AND ".join(clauses),tuple(params)).fetchone()
        if not row: return None
        data=dict(row); data["result"]=json.loads(data.pop("result_json")) if data.get("result_json") else None
        data.pop("request_json",None)
        return data


    def create_portal_user(self, user: dict[str, Any]) -> None:
        with self._lock, self._conn() as conn:
            conn.execute("""INSERT INTO portal_users(id,email,password_hash,display_name,tenant_id,company_code,shop_id,role,enabled,created_at)
                VALUES(?,?,?,?,?,?,?,?,1,?)""",(user["id"],user["email"],user["password_hash"],user["display_name"],
                user["tenant_id"],user.get("company_code"),user["shop_id"],user["role"],user["created_at"]))

    def portal_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row=conn.execute("SELECT * FROM portal_users WHERE lower(email)=lower(?) AND enabled=1",(email,)).fetchone()
        return dict(row) if row else None

    def create_portal_session(self, session: dict[str, Any]) -> None:
        with self._lock,self._conn() as conn:
            conn.execute("""INSERT INTO portal_sessions(session_id,token_hash,tenant_id,company_code,shop_id,user_id,display_name,role,created_at,expires_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""",(session["session_id"],session["token_hash"],session["tenant_id"],session.get("company_code"),
                session["shop_id"],session.get("user_id"),session.get("display_name"),session["role"],session["created_at"],session["expires_at"]))

    def portal_session_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row=conn.execute("SELECT * FROM portal_sessions WHERE token_hash=?",(token_hash,)).fetchone()
        return dict(row) if row else None
    def upsert_crm_person_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        now=self.now()
        with self._lock,self._conn() as conn:
            conn.execute("""INSERT INTO crm_person_mappings(tenant_id,shop_id,local_person_id,crm_user_id,employee_code,break_master_id,enabled,created_at,updated_at)
                VALUES(?,?,?,?,?,?,1,?,?) ON CONFLICT(tenant_id,shop_id,local_person_id) DO UPDATE SET
                crm_user_id=excluded.crm_user_id,employee_code=excluded.employee_code,break_master_id=excluded.break_master_id,enabled=1,updated_at=excluded.updated_at""",
                (mapping["tenant_id"],mapping["shop_id"],mapping["local_person_id"],mapping["crm_user_id"],mapping.get("employee_code"),mapping.get("break_master_id"),now,now))
        return self.crm_person_mapping(mapping["tenant_id"],mapping["shop_id"],mapping["local_person_id"])

    def crm_person_mapping(self, tenant_id: str, shop_id: str, local_person_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row=conn.execute("SELECT * FROM crm_person_mappings WHERE tenant_id=? AND shop_id=? AND local_person_id=? AND enabled=1",(tenant_id,shop_id,local_person_id)).fetchone()
        return dict(row) if row else None

    def list_crm_person_mappings(self, tenant_id: str, shop_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows=conn.execute("SELECT * FROM crm_person_mappings WHERE tenant_id=? AND shop_id=? AND enabled=1 ORDER BY employee_code,local_person_id",(tenant_id,shop_id)).fetchall()
        return [dict(row) for row in rows]

