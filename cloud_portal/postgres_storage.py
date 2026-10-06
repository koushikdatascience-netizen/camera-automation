from __future__ import annotations

import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import create_engine, text


class PostgresPortalStore:
    """Production cloud store. Edge machines remain SQLite/offline-first."""

    def __init__(self, database_url: str | None = None):
        self.database_url = (database_url or os.getenv("SNAPKEY_DATABASE_URL", "")).strip()
        if not self.database_url:
            raise RuntimeError("SNAPKEY_DATABASE_URL is required for PostgreSQL portal storage")
        self.engine = create_engine(
            self.database_url,
            pool_pre_ping=True,
            pool_size=int(os.getenv("SNAPKEY_DB_POOL_SIZE", "10")),
            max_overflow=int(os.getenv("SNAPKEY_DB_MAX_OVERFLOW", "20")),
            pool_recycle=int(os.getenv("SNAPKEY_DB_POOL_RECYCLE_SECONDS", "1800")),
        )
        self._init()

    @contextmanager
    def _conn(self):
        with self.engine.begin() as conn:
            yield conn

    def _init(self) -> None:
        statements = [
            """CREATE TABLE IF NOT EXISTS tenants(id TEXT PRIMARY KEY, name TEXT, created_at TIMESTAMPTZ NOT NULL)""",
            """ALTER TABLE tenants ADD COLUMN IF NOT EXISTS client_id TEXT""",
            """ALTER TABLE tenants ADD COLUMN IF NOT EXISTS tenant_code TEXT""",
            """ALTER TABLE tenants ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_tenants_client_id ON tenants(lower(client_id)) WHERE client_id IS NOT NULL""",
            """CREATE TABLE IF NOT EXISTS sites(id TEXT NOT NULL, tenant_id TEXT NOT NULL, name TEXT, created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(id,tenant_id))""",
            """ALTER TABLE sites ADD COLUMN IF NOT EXISTS counter_code TEXT""",
            """ALTER TABLE sites ADD COLUMN IF NOT EXISTS company_code TEXT""",
            """ALTER TABLE sites ADD COLUMN IF NOT EXISTS active BOOLEAN NOT NULL DEFAULT TRUE""",
            """CREATE INDEX IF NOT EXISTS idx_sites_counter_scope ON sites(tenant_id,counter_code,active)""",
            """CREATE TABLE IF NOT EXISTS edge_machines(id TEXT NOT NULL, tenant_id TEXT NOT NULL, site_id TEXT NOT NULL, last_seen_at TIMESTAMPTZ NOT NULL, company_code TEXT, shop_id TEXT, PRIMARY KEY(id,tenant_id,site_id))""",
            """CREATE TABLE IF NOT EXISTS edge_heartbeats(tenant_id TEXT NOT NULL, site_id TEXT NOT NULL, edge_id TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL, status_json JSONB NOT NULL, company_code TEXT, shop_id TEXT, PRIMARY KEY(tenant_id,site_id,edge_id))""",
            """ALTER TABLE edge_machines ADD COLUMN IF NOT EXISTS company_code TEXT""",
            """ALTER TABLE edge_machines ADD COLUMN IF NOT EXISTS shop_id TEXT""",
            """ALTER TABLE edge_heartbeats ADD COLUMN IF NOT EXISTS company_code TEXT""",
            """ALTER TABLE edge_heartbeats ADD COLUMN IF NOT EXISTS shop_id TEXT""",
            """CREATE TABLE IF NOT EXISTS edge_credentials(token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, company_code TEXT, shop_id TEXT NOT NULL, site_id TEXT NOT NULL, edge_id TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS edge_activation_codes(code_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, company_code TEXT, shop_id TEXT NOT NULL, created_by TEXT, created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL, consumed_at TIMESTAMPTZ, consumed_machine_code TEXT)""",
            """CREATE INDEX IF NOT EXISTS idx_edge_activation_scope ON edge_activation_codes(tenant_id,shop_id,created_at DESC)""",
            """DROP INDEX IF EXISTS idx_edge_credentials_identity""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_edge_credentials_active_identity ON edge_credentials(tenant_id,shop_id,site_id,edge_id) WHERE enabled=TRUE""",
            """CREATE TABLE IF NOT EXISTS edge_events(id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, company_code TEXT, shop_id TEXT, site_id TEXT NOT NULL, edge_id TEXT NOT NULL, store_id TEXT, camera_id TEXT, event_type TEXT NOT NULL, event_time TIMESTAMPTZ NOT NULL, received_at TIMESTAMPTZ NOT NULL, payload_json JSONB NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS idx_edge_events_tenant_time ON edge_events(tenant_id,site_id,event_time DESC)""",
            """CREATE INDEX IF NOT EXISTS idx_edge_events_shop_time ON edge_events(tenant_id,shop_id,event_time DESC)""",
            """CREATE INDEX IF NOT EXISTS idx_edge_events_type ON edge_events(tenant_id,event_type,event_time DESC)""",
            """CREATE TABLE IF NOT EXISTS camera_configs(
                tenant_id TEXT NOT NULL, company_code TEXT, shop_id TEXT NOT NULL, site_id TEXT NOT NULL,
                edge_id TEXT NOT NULL, camera_id TEXT NOT NULL, name TEXT NOT NULL, source_type TEXT NOT NULL,
                source TEXT NOT NULL, camera_role TEXT NOT NULL, camera_zone TEXT, crowd_threshold INTEGER NOT NULL DEFAULT 10,
                enabled BOOLEAN NOT NULL DEFAULT TRUE, features_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                settings_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(tenant_id,shop_id,edge_id,camera_id))""",
            """CREATE INDEX IF NOT EXISTS idx_camera_configs_scope ON camera_configs(tenant_id,shop_id,edge_id)""",
            """CREATE TABLE IF NOT EXISTS edge_commands(
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL, edge_id TEXT NOT NULL,
                command_type TEXT NOT NULL, request_json JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'PENDING',
                result_json JSONB, created_at TIMESTAMPTZ NOT NULL, claimed_at TIMESTAMPTZ, completed_at TIMESTAMPTZ)""",
            """CREATE INDEX IF NOT EXISTS idx_edge_commands_pending ON edge_commands(tenant_id,shop_id,edge_id,status,created_at)""",
            """CREATE TABLE IF NOT EXISTS portal_sessions(
                session_id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, tenant_id TEXT NOT NULL, company_code TEXT,
                shop_id TEXT NOT NULL, user_id TEXT, display_name TEXT, role TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS idx_portal_sessions_token ON portal_sessions(token_hash,expires_at)""",
            """CREATE TABLE IF NOT EXISTS portal_users(
                id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, password_hash TEXT NOT NULL, display_name TEXT NOT NULL,
                tenant_id TEXT NOT NULL, company_code TEXT, shop_id TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'OWNER',
                enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS idx_portal_users_email ON portal_users(email)""",
            """CREATE TABLE IF NOT EXISTS portal_user_sites(
                user_id TEXT NOT NULL, tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL,
                role TEXT NOT NULL DEFAULT 'USER', enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                PRIMARY KEY(user_id,tenant_id,shop_id))""",
            """CREATE INDEX IF NOT EXISTS idx_portal_user_sites_scope ON portal_user_sites(tenant_id,shop_id,enabled)""",
            """INSERT INTO portal_user_sites(user_id,tenant_id,shop_id,role,enabled,created_at)
                SELECT id,tenant_id,shop_id,role,TRUE,created_at FROM portal_users
                ON CONFLICT(user_id,tenant_id,shop_id) DO NOTHING""",
            """CREATE TABLE IF NOT EXISTS cloud_personnel(
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL, employee_code TEXT NOT NULL,
                full_name TEXT NOT NULL, role TEXT NOT NULL, phone TEXT, email TEXT, active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                UNIQUE(tenant_id,shop_id,employee_code))""",
            """CREATE INDEX IF NOT EXISTS idx_cloud_personnel_scope ON cloud_personnel(tenant_id,shop_id,active)""",
            """CREATE TABLE IF NOT EXISTS cloud_face_profiles(
                id TEXT PRIMARY KEY, person_id TEXT NOT NULL, tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL,
                embedding_json JSONB NOT NULL, quality DOUBLE PRECISION NOT NULL, image_path TEXT,
                created_at TIMESTAMPTZ NOT NULL, FOREIGN KEY(person_id) REFERENCES cloud_personnel(id) ON DELETE CASCADE)""",
            """CREATE INDEX IF NOT EXISTS idx_cloud_faces_person ON cloud_face_profiles(tenant_id,shop_id,person_id)""",
            """CREATE TABLE IF NOT EXISTS crm_person_mappings(
                tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL, local_person_id TEXT NOT NULL,
                crm_user_id TEXT NOT NULL, employee_code TEXT, break_master_id TEXT, enabled BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMPTZ NOT NULL, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(tenant_id,shop_id,local_person_id))""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_crm_person_user ON crm_person_mappings(tenant_id,shop_id,crm_user_id)""",
            """CREATE TABLE IF NOT EXISTS attendance_policies(
                tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL,
                grace_period_minutes INTEGER NOT NULL DEFAULT 15,
                allowed_break_minutes INTEGER NOT NULL DEFAULT 60,
                total_working_minutes INTEGER NOT NULL DEFAULT 480,
                max_logoff_time TEXT NOT NULL DEFAULT '21:30',
                absence_auto_logout_enabled BOOLEAN NOT NULL DEFAULT TRUE,
                timezone TEXT NOT NULL DEFAULT 'Asia/Kolkata',
                email_recipients_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                whatsapp_recipients_json JSONB NOT NULL DEFAULT '[]'::jsonb,
                updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(tenant_id,shop_id))""",
            """CREATE TABLE IF NOT EXISTS attendance_activity(
                id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL,
                crm_user_id TEXT NOT NULL, local_person_id TEXT,
                activity_type TEXT NOT NULL, occurred_at TIMESTAMPTZ NOT NULL,
                reason_code TEXT, source TEXT NOT NULL, camera_id TEXT,
                evidence_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb,
                created_at TIMESTAMPTZ NOT NULL)""",
            """CREATE INDEX IF NOT EXISTS idx_attendance_activity_daily
                ON attendance_activity(tenant_id,shop_id,crm_user_id,occurred_at DESC)""",
            """CREATE TABLE IF NOT EXISTS attendance_presence(
                tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL, local_person_id TEXT NOT NULL,
                crm_user_id TEXT NOT NULL, checked_in BOOLEAN NOT NULL DEFAULT FALSE,
                on_break BOOLEAN NOT NULL DEFAULT FALSE, last_seen_at TIMESTAMPTZ NOT NULL,
                last_camera_id TEXT, last_recognition_event_id TEXT,
                checkout_claimed_at TIMESTAMPTZ, updated_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY(tenant_id,shop_id,local_person_id))""",
            """CREATE INDEX IF NOT EXISTS idx_attendance_presence_due
                ON attendance_presence(checked_in,on_break,last_seen_at)""",
        ]
        with self._conn() as conn:
            for statement in statements:
                conn.execute(text(statement))

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    def create_edge_activation_code(self, item: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(text("""INSERT INTO edge_activation_codes(code_hash,tenant_id,company_code,shop_id,created_by,created_at,expires_at)
                VALUES(:hash,:tenant,:company,:shop,:created_by,:created_at,:expires_at)"""), {
                "hash": item["code_hash"], "tenant": item["tenant_id"], "company": item.get("company_code"),
                "shop": item["shop_id"], "created_by": item.get("created_by"),
                "created_at": item["created_at"], "expires_at": item["expires_at"],
            })

    def consume_edge_activation_code(self, code_hash: str, machine_code: str) -> dict[str, Any] | None:
        now = self.now()
        with self._conn() as conn:
            row = conn.execute(text("""UPDATE edge_activation_codes
                SET consumed_at=:now,consumed_machine_code=:machine
                WHERE code_hash=:hash AND consumed_at IS NULL AND expires_at>:now
                RETURNING tenant_id,company_code,shop_id,expires_at"""),
                {"hash": code_hash, "machine": machine_code, "now": now}).mappings().first()
        return dict(row) if row else None

    def resolve_edge_credential(self, token_hash: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(text("""SELECT tenant_id,company_code,shop_id,site_id,edge_id
                FROM edge_credentials WHERE token_hash=:token_hash AND enabled=TRUE"""),
                {"token_hash": token_hash}).mappings().first()
        return dict(row) if row else None

    def provision_edge_credential(self, token_hash: str, tenant_id: str, company_code: str | None,
                                  shop_id: str, site_id: str, edge_id: str) -> None:
        with self._conn() as conn:
            conn.execute(text("""UPDATE edge_credentials SET enabled=FALSE
                WHERE tenant_id=:tenant AND shop_id=:shop AND site_id=:site AND edge_id=:edge
                AND token_hash<>:token_hash AND enabled=TRUE"""),
                {"token_hash":token_hash,"tenant":tenant_id,"shop":shop_id,"site":site_id,"edge":edge_id})
            conn.execute(text("""INSERT INTO edge_credentials(token_hash,tenant_id,company_code,shop_id,site_id,edge_id,enabled,created_at)
                VALUES(:token_hash,:tenant,:company,:shop,:site,:edge,TRUE,:now)
                ON CONFLICT(token_hash) DO UPDATE SET tenant_id=EXCLUDED.tenant_id,company_code=EXCLUDED.company_code,
                shop_id=EXCLUDED.shop_id,site_id=EXCLUDED.site_id,edge_id=EXCLUDED.edge_id,enabled=TRUE"""),
                {"token_hash":token_hash,"tenant":tenant_id,"company":company_code,"shop":shop_id,
                 "site":site_id,"edge":edge_id,"now":self.now()})

    def revoke_edge_credentials(self, tenant_id: str, shop_id: str, edge_id: str) -> int:
        with self._conn() as conn:
            result = conn.execute(text("""UPDATE edge_credentials SET enabled=FALSE
                WHERE tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge AND enabled=TRUE"""),
                {"tenant":tenant_id,"shop":shop_id,"edge":edge_id})
        return int(result.rowcount or 0)

    def delete_edge(self, tenant_id: str, shop_id: str, edge_id: str) -> bool:
        """Remove a stale edge registration while preserving historical events."""
        with self._conn() as conn:
            params={"tenant":tenant_id,"shop":shop_id,"edge":edge_id}
            rows=conn.execute(text("""SELECT id,site_id FROM edge_machines
                WHERE tenant_id=:tenant AND shop_id=:shop AND id=:edge"""),params).mappings().all()
            if not rows:
                return False
            # Revoke/delete credentials first so a removed installation cannot silently
            # re-register; it must be explicitly paired again.
            conn.execute(text("""DELETE FROM edge_credentials
                WHERE tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge"""),params)
            conn.execute(text("""DELETE FROM edge_commands
                WHERE tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge"""),params)
            conn.execute(text("""DELETE FROM camera_configs
                WHERE tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge"""),params)
            conn.execute(text("""DELETE FROM edge_heartbeats
                WHERE tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge"""),params)
            conn.execute(text("""DELETE FROM edge_machines
                WHERE tenant_id=:tenant AND shop_id=:shop AND id=:edge"""),params)
        return True

    def ingest_event(self, envelope: dict[str, Any]) -> dict[str, Any]:
        tenant_id=str(envelope["tenant_id"]); site_id=str(envelope["site_id"]); edge_id=str(envelope["edge_id"]); event_id=str(envelope["event_id"]); now=self.now()
        with self._conn() as conn:
            conn.execute(text("INSERT INTO tenants(id,name,created_at) VALUES(:id,:name,:now) ON CONFLICT(id) DO NOTHING"),{"id":tenant_id,"name":tenant_id,"now":now})
            conn.execute(text("INSERT INTO sites(id,tenant_id,name,created_at) VALUES(:id,:tenant,:name,:now) ON CONFLICT(id,tenant_id) DO NOTHING"),{"id":site_id,"tenant":tenant_id,"name":site_id,"now":now})
            conn.execute(text("INSERT INTO edge_machines(id,tenant_id,site_id,last_seen_at) VALUES(:id,:tenant,:site,:now) ON CONFLICT(id,tenant_id,site_id) DO UPDATE SET last_seen_at=EXCLUDED.last_seen_at"),{"id":edge_id,"tenant":tenant_id,"site":site_id,"now":now})
            inserted = conn.execute(text("""INSERT INTO edge_events(id,tenant_id,company_code,shop_id,site_id,edge_id,store_id,camera_id,event_type,event_time,received_at,payload_json)
                VALUES(:id,:tenant,:company,:shop,:site,:edge,:store,:camera,:type,:event_time,:received,CAST(:payload AS JSONB))
                ON CONFLICT(id) DO NOTHING"""),{
                "id":event_id,"tenant":tenant_id,"company":envelope.get("company_code"),"shop":envelope.get("shop_id") or site_id,
                "site":site_id,"edge":edge_id,"store":envelope.get("store_id"),"camera":envelope.get("camera_id"),"type":envelope.get("event_type"),
                "event_time":envelope.get("event_time"),"received":now,"payload":json.dumps(envelope)}).rowcount > 0
        return {"ok":True,"event_id":event_id,"tenant_id":tenant_id,"site_id":site_id,"inserted":bool(inserted)}

    def record_portal_event(self, item: dict[str, Any]) -> dict[str, Any]:
        envelope=dict(item); envelope.setdefault("store_id",item.get("shop_id")); envelope.setdefault("payload",{})
        return self.ingest_event(envelope)

    def list_events(self, tenant_id: str, site_id: str | None = None, event_type: str | None = None, limit: int = 100, shop_id: str | None = None):
        clauses=["tenant_id=:tenant"]; params={"tenant":tenant_id,"limit":max(1,min(500,int(limit)))}
        if shop_id: clauses.append("shop_id=:shop"); params["shop"]=shop_id
        if site_id: clauses.append("site_id=:site"); params["site"]=site_id
        if event_type: clauses.append("event_type=:event_type"); params["event_type"]=event_type
        query="SELECT * FROM edge_events WHERE "+" AND ".join(clauses)+" ORDER BY event_time DESC LIMIT :limit"
        with self._conn() as conn:
            rows=conn.execute(text(query),params).mappings().all()
            return [dict(r) | {"payload": r["payload_json"] if isinstance(r["payload_json"],dict) else json.loads(r["payload_json"])} for r in rows]

    def get_event(self, tenant_id: str, shop_id: str, event_id: str):
        with self._conn() as conn:
            row = conn.execute(text("SELECT * FROM edge_events WHERE tenant_id=:t AND shop_id=:s AND id=:id"), {"t": tenant_id, "s": shop_id, "id": event_id}).mappings().first()
            if not row:
                return None
            payload = row["payload_json"]
            return dict(row) | {"payload": payload if isinstance(payload, dict) else json.loads(payload)}

    def tenant_summary(self, tenant_id: str, shop_id: str | None = None):
        with self._conn() as conn:
            if shop_id:
                sites=conn.execute(text("SELECT COUNT(DISTINCT site_id) FROM edge_machines WHERE tenant_id=:t AND shop_id=:s"),{"t":tenant_id,"s":shop_id}).scalar_one()
                edges=conn.execute(text("SELECT COUNT(*) FROM edge_machines WHERE tenant_id=:t AND shop_id=:s"),{"t":tenant_id,"s":shop_id}).scalar_one()
                events=conn.execute(text("SELECT event_type,COUNT(*) AS count FROM edge_events WHERE tenant_id=:t AND shop_id=:s GROUP BY event_type"),{"t":tenant_id,"s":shop_id}).mappings().all()
            else:
                sites=conn.execute(text("SELECT COUNT(*) FROM sites WHERE tenant_id=:t"),{"t":tenant_id}).scalar_one()
                edges=conn.execute(text("SELECT COUNT(*) FROM edge_machines WHERE tenant_id=:t"),{"t":tenant_id}).scalar_one()
                events=conn.execute(text("SELECT event_type,COUNT(*) AS count FROM edge_events WHERE tenant_id=:t GROUP BY event_type"),{"t":tenant_id}).mappings().all()
        return {"tenant_id":tenant_id,"sites":sites,"edges":edges,"events":{r["event_type"]:r["count"] for r in events}}


    def create_cloud_person(self, item: dict[str, Any]) -> dict[str, Any]:
        now=self.now()
        with self._conn() as conn:
            row=conn.execute(text("""INSERT INTO cloud_personnel(id,tenant_id,shop_id,employee_code,full_name,role,phone,email,active,created_at,updated_at)
                VALUES(:id,:tenant,:shop,:code,:name,:role,:phone,:email,TRUE,:now,:now) RETURNING *"""),
                {"id":item["id"],"tenant":item["tenant_id"],"shop":item["shop_id"],"code":item["employee_code"],
                 "name":item["full_name"],"role":item["role"],"phone":item.get("phone"),"email":item.get("email"),"now":now}).mappings().one()
        return dict(row)

    def upsert_crm_cloud_person(self, item: dict[str, Any]) -> dict[str, Any]:
        """Mirror CRM identity into Camera Eye without creating a second employee identity."""
        now=self.now()
        with self._conn() as conn:
            row=conn.execute(text("""INSERT INTO cloud_personnel(id,tenant_id,shop_id,employee_code,full_name,role,phone,email,active,created_at,updated_at)
                VALUES(:id,:tenant,:shop,:code,:name,:role,:phone,:email,:active,:now,:now)
                ON CONFLICT(id) DO UPDATE SET tenant_id=EXCLUDED.tenant_id,shop_id=EXCLUDED.shop_id,
                employee_code=EXCLUDED.employee_code,full_name=EXCLUDED.full_name,role=EXCLUDED.role,
                phone=EXCLUDED.phone,email=EXCLUDED.email,active=EXCLUDED.active,updated_at=EXCLUDED.updated_at
                RETURNING *"""),
                {"id":item["id"],"tenant":item["tenant_id"],"shop":item["shop_id"],"code":item["employee_code"],
                 "name":item["full_name"],"role":item["role"],"phone":item.get("phone"),"email":item.get("email"),
                 "active":bool(item.get("active",True)),"now":now}).mappings().one()
        return dict(row)

    def list_cloud_people(self, tenant_id: str, shop_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows=conn.execute(text("""SELECT p.*,COUNT(f.id) AS face_count FROM cloud_personnel p
                LEFT JOIN cloud_face_profiles f ON f.person_id=p.id
                WHERE p.tenant_id=:tenant AND p.shop_id=:shop GROUP BY p.id ORDER BY p.full_name"""),
                {"tenant":tenant_id,"shop":shop_id}).mappings().all()
        return [dict(r) for r in rows]

    def get_cloud_person(self, tenant_id: str, shop_id: str, person_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row=conn.execute(text("SELECT * FROM cloud_personnel WHERE tenant_id=:tenant AND shop_id=:shop AND id=:id"),
                {"tenant":tenant_id,"shop":shop_id,"id":person_id}).mappings().first()
        return dict(row) if row else None

    def update_cloud_person(self, tenant_id: str, shop_id: str, person_id: str, changes: dict[str, Any]) -> dict[str, Any] | None:
        allowed={k:v for k,v in changes.items() if k in {"full_name","role","phone","email","active"} and v is not None}
        if allowed:
            allowed["updated_at"]=self.now(); params={**allowed,"tenant":tenant_id,"shop":shop_id,"id":person_id}
            sets=",".join(f"{key}=:{key}" for key in allowed)
            with self._conn() as conn: conn.execute(text(f"UPDATE cloud_personnel SET {sets} WHERE tenant_id=:tenant AND shop_id=:shop AND id=:id"),params)
        return self.get_cloud_person(tenant_id,shop_id,person_id)

    def add_cloud_face(self, item: dict[str, Any]) -> dict[str, Any]:
        with self._conn() as conn:
            row=conn.execute(text("""INSERT INTO cloud_face_profiles(id,person_id,tenant_id,shop_id,embedding_json,quality,image_path,created_at)
                VALUES(:id,:person,:tenant,:shop,CAST(:embedding AS JSONB),:quality,:image_path,:created) RETURNING id,person_id,quality,image_path,created_at"""),
                {"id":item["id"],"person":item["person_id"],"tenant":item["tenant_id"],"shop":item["shop_id"],
                 "embedding":json.dumps(item["embedding"]),"quality":item["quality"],"image_path":item.get("image_path"),"created":self.now()}).mappings().one()
        return dict(row)

    def list_cloud_faces(self, tenant_id: str, shop_id: str, person_id: str, include_embedding: bool=False) -> list[dict[str, Any]]:
        cols="id,person_id,quality,image_path,created_at"+(",embedding_json" if include_embedding else "")
        with self._conn() as conn:
            rows=conn.execute(text(f"SELECT {cols} FROM cloud_face_profiles WHERE tenant_id=:tenant AND shop_id=:shop AND person_id=:person ORDER BY created_at DESC"),
                {"tenant":tenant_id,"shop":shop_id,"person":person_id}).mappings().all()
        out=[]
        for row in rows:
            item=dict(row)
            if include_embedding:
                item["embedding"]=item.pop("embedding_json")
            out.append(item)
        return out

    def delete_cloud_face(self, tenant_id: str, shop_id: str, person_id: str, face_id: str) -> bool:
        with self._conn() as conn:
            result=conn.execute(text("DELETE FROM cloud_face_profiles WHERE tenant_id=:tenant AND shop_id=:shop AND person_id=:person AND id=:face"),
                {"tenant":tenant_id,"shop":shop_id,"person":person_id,"face":face_id})
        return bool(result.rowcount)

    def record_heartbeat(self, payload: dict[str, Any]):
        tenant=str(payload["tenant_id"]); site=str(payload["site_id"]); edge=str(payload["edge_id"]); now=self.now()
        company=payload.get("company_code"); shop=str(payload.get("shop_id") or site)
        with self._conn() as conn:
            conn.execute(text("INSERT INTO tenants(id,name,created_at) VALUES(:id,:id,:now) ON CONFLICT(id) DO NOTHING"),{"id":tenant,"now":now})
            conn.execute(text("INSERT INTO sites(id,tenant_id,name,created_at) VALUES(:site,:tenant,:site,:now) ON CONFLICT(id,tenant_id) DO NOTHING"),{"site":site,"tenant":tenant,"now":now})
            conn.execute(text("""INSERT INTO edge_machines(id,tenant_id,site_id,last_seen_at,company_code,shop_id)
                VALUES(:edge,:tenant,:site,:now,:company,:shop)
                ON CONFLICT(id,tenant_id,site_id) DO UPDATE SET last_seen_at=EXCLUDED.last_seen_at,
                company_code=EXCLUDED.company_code,shop_id=EXCLUDED.shop_id"""),
                {"edge":edge,"tenant":tenant,"site":site,"now":now,"company":company,"shop":shop})
            conn.execute(text("""INSERT INTO edge_heartbeats(tenant_id,site_id,edge_id,received_at,status_json,company_code,shop_id)
                VALUES(:tenant,:site,:edge,:now,CAST(:status AS JSONB),:company,:shop)
                ON CONFLICT(tenant_id,site_id,edge_id) DO UPDATE SET received_at=EXCLUDED.received_at,
                status_json=EXCLUDED.status_json,company_code=EXCLUDED.company_code,shop_id=EXCLUDED.shop_id"""),
                {"tenant":tenant,"site":site,"edge":edge,"now":now,"status":json.dumps(payload.get("status") or {}),
                 "company":company,"shop":shop})
        return {"ok":True,"tenant_id":tenant,"site_id":site,"edge_id":edge,"received_at":now.isoformat()}

    def list_edges(self, tenant_id: str, shop_id: str | None = None) -> list[dict[str, Any]]:
        clauses=["m.tenant_id=:tenant"]; params: dict[str, Any]={"tenant":tenant_id}
        if shop_id:
            clauses.append("m.shop_id=:shop"); params["shop"]=shop_id
        query="""SELECT m.id AS edge_id,m.tenant_id,m.company_code,m.shop_id,m.site_id,m.last_seen_at,
                        h.received_at,h.status_json
                 FROM edge_machines m
                 LEFT JOIN edge_heartbeats h ON h.tenant_id=m.tenant_id AND h.site_id=m.site_id AND h.edge_id=m.id
                 WHERE """+" AND ".join(clauses)+" ORDER BY m.last_seen_at DESC"
        with self._conn() as conn:
            rows=conn.execute(text(query),params).mappings().all()
        result=[]
        for row in rows:
            data=dict(row)
            for key in ("last_seen_at","received_at"):
                if hasattr(data.get(key),"isoformat"): data[key]=data[key].isoformat()
            status=data.pop("status_json") or {}
            data["status"]=status if isinstance(status,dict) else json.loads(status)
            result.append(data)
        return result


    def upsert_camera(self, camera: dict[str, Any]) -> dict[str, Any]:
        now = self.now()
        params = {
            "tenant": camera["tenant_id"], "company": camera.get("company_code"), "shop": camera["shop_id"],
            "site": camera["site_id"], "edge": camera["edge_id"], "camera": camera["camera_id"],
            "name": camera["name"], "source_type": camera.get("source_type", "rtsp"), "source": camera["source"],
            "role": camera.get("camera_role", "GENERAL"), "zone": camera.get("camera_zone"),
            "threshold": int(camera.get("crowd_threshold", 10)), "enabled": bool(camera.get("enabled", True)),
            "features": json.dumps(camera.get("features") or {}), "settings": json.dumps(camera.get("settings") or {}),
            "now": now,
        }
        with self._conn() as conn:
            conn.execute(text("""INSERT INTO camera_configs(
                tenant_id,company_code,shop_id,site_id,edge_id,camera_id,name,source_type,source,camera_role,
                camera_zone,crowd_threshold,enabled,features_json,settings_json,created_at,updated_at)
                VALUES(:tenant,:company,:shop,:site,:edge,:camera,:name,:source_type,:source,:role,:zone,:threshold,:enabled,
                CAST(:features AS JSONB),CAST(:settings AS JSONB),:now,:now)
                ON CONFLICT(tenant_id,shop_id,edge_id,camera_id) DO UPDATE SET
                company_code=EXCLUDED.company_code,site_id=EXCLUDED.site_id,name=EXCLUDED.name,
                source_type=EXCLUDED.source_type,source=EXCLUDED.source,camera_role=EXCLUDED.camera_role,
                camera_zone=EXCLUDED.camera_zone,crowd_threshold=EXCLUDED.crowd_threshold,enabled=EXCLUDED.enabled,
                features_json=EXCLUDED.features_json,settings_json=EXCLUDED.settings_json,updated_at=EXCLUDED.updated_at"""), params)
        return self.get_camera(camera["tenant_id"], camera["shop_id"], camera["edge_id"], camera["camera_id"])

    def get_camera(self, tenant_id: str, shop_id: str, edge_id: str, camera_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(text("""SELECT * FROM camera_configs
                WHERE tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge AND camera_id=:camera"""),
                {"tenant":tenant_id,"shop":shop_id,"edge":edge_id,"camera":camera_id}).mappings().first()
        return self._camera_row(row) if row else None

    def list_cameras(self, tenant_id: str, shop_id: str | None = None, edge_id: str | None = None) -> list[dict[str, Any]]:
        clauses = ["tenant_id=:tenant"]; params: dict[str, Any] = {"tenant": tenant_id}
        if shop_id:
            clauses.append("shop_id=:shop"); params["shop"] = shop_id
        if edge_id:
            clauses.append("edge_id=:edge"); params["edge"] = edge_id
        with self._conn() as conn:
            rows = conn.execute(text("SELECT * FROM camera_configs WHERE "+" AND ".join(clauses)+" ORDER BY name,camera_id"), params).mappings().all()
        return [self._camera_row(row) for row in rows]

    def delete_camera(self, tenant_id: str, shop_id: str, edge_id: str, camera_id: str) -> bool:
        with self._conn() as conn:
            result = conn.execute(text("""DELETE FROM camera_configs
                WHERE tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge AND camera_id=:camera"""),
                {"tenant":tenant_id,"shop":shop_id,"edge":edge_id,"camera":camera_id})
        return bool(result.rowcount)

    @staticmethod
    def _camera_row(row) -> dict[str, Any]:
        data = dict(row)
        for key in ("created_at", "updated_at"):
            if hasattr(data.get(key), "isoformat"):
                data[key] = data[key].isoformat()
        for key in ("features_json", "settings_json"):
            value = data.pop(key)
            data["features" if key == "features_json" else "settings"] = value if isinstance(value, dict) else json.loads(value or "{}")
        return data


    def create_edge_command(self, command: dict[str, Any]) -> dict[str, Any]:
        import uuid
        command_id = str(uuid.uuid4()); now = self.now()
        with self._conn() as conn:
            conn.execute(text("""INSERT INTO edge_commands(id,tenant_id,shop_id,edge_id,command_type,request_json,status,created_at)
                VALUES(:id,:tenant,:shop,:edge,:type,CAST(:request AS JSONB),'PENDING',:now)"""),
                {"id":command_id,"tenant":command["tenant_id"],"shop":command["shop_id"],"edge":command["edge_id"],
                 "type":command["command_type"],"request":json.dumps(command.get("request") or {}),"now":now})
        return {"id":command_id,"status":"PENDING"}

    def claim_edge_commands(self, tenant_id: str, shop_id: str, edge_id: str, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as conn:
            conn.execute(text("UPDATE edge_commands SET status='PENDING' WHERE tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge AND status='CLAIMED' AND claimed_at < NOW() - INTERVAL '2 minutes'"), {"tenant": tenant_id, "shop": shop_id, "edge": edge_id})
            rows=conn.execute(text("""UPDATE edge_commands SET status='CLAIMED',claimed_at=:now
                WHERE id IN (SELECT id FROM edge_commands WHERE tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge
                AND status='PENDING' ORDER BY created_at LIMIT :limit FOR UPDATE SKIP LOCKED)
                RETURNING id,command_type,request_json"""),
                {"now":self.now(),"tenant":tenant_id,"shop":shop_id,"edge":edge_id,"limit":max(1,min(20,int(limit)))}).mappings().all()
        return [{"id":r["id"],"command_type":r["command_type"],"request":r["request_json"]} for r in rows]

    def complete_edge_command(self, command_id: str, tenant_id: str, shop_id: str, edge_id: str,
                              status: str, result: dict[str, Any]) -> bool:
        with self._conn() as conn:
            out=conn.execute(text("""UPDATE edge_commands SET status=:status,result_json=CAST(:result AS JSONB),completed_at=:now
                WHERE id=:id AND tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge AND status='CLAIMED'"""),
                {"status":status,"result":json.dumps(result),"now":self.now(),"id":command_id,
                 "tenant":tenant_id,"shop":shop_id,"edge":edge_id})
        return bool(out.rowcount)

    def get_edge_command(self, command_id: str, tenant_id: str, shop_id: str | None = None) -> dict[str, Any] | None:
        clauses=["id=:id","tenant_id=:tenant"]
        params: dict[str, Any]={"id":command_id,"tenant":tenant_id}
        if shop_id:
            clauses.append("shop_id=:shop")
            params["shop"]=shop_id
        with self._conn() as conn:
            row=conn.execute(text("""SELECT id,tenant_id,shop_id,edge_id,command_type,status,result_json,created_at,claimed_at,completed_at
                FROM edge_commands WHERE """+" AND ".join(clauses)),params).mappings().first()
        if not row: return None
        data=dict(row)
        for key in ("created_at","claimed_at","completed_at"):
            if hasattr(data.get(key),"isoformat"): data[key]=data[key].isoformat()
        data["result"]=data.pop("result_json")
        return data


    def create_portal_user(self, user: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(text("""INSERT INTO portal_users(id,email,password_hash,display_name,tenant_id,company_code,shop_id,role,enabled,created_at)
                VALUES(:id,:email,:password_hash,:display_name,:tenant_id,:company_code,:shop_id,:role,TRUE,:created_at)"""), user)

    def portal_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row=conn.execute(text("SELECT * FROM portal_users WHERE lower(email)=lower(:email) AND enabled=TRUE"),{"email":email}).mappings().first()
        return dict(row) if row else None

    def client_login_options(self, client_id: str) -> dict[str, Any]:
        """Return non-secret login choices for a first-class Camera Eye client."""
        client=(client_id or "").strip()
        with self._conn() as conn:
            users=conn.execute(text("""SELECT DISTINCT u.id,u.display_name
                FROM tenants t
                JOIN portal_user_sites s ON s.tenant_id=t.id AND s.enabled=TRUE
                JOIN portal_users u ON u.id=s.user_id AND u.enabled=TRUE
                WHERE t.active=TRUE AND lower(COALESCE(t.client_id,''))=lower(:client)
                ORDER BY u.display_name,u.id"""),{"client":client}).mappings().all()
            sites=conn.execute(text("""SELECT DISTINCT s.shop_id,
                       COALESCE(NULLIF(si.name,''),NULLIF(si.counter_code,''),s.shop_id) AS name,
                       COALESCE(NULLIF(si.counter_code,''),s.shop_id) AS counter_code
                FROM tenants t
                JOIN portal_user_sites s ON s.tenant_id=t.id AND s.enabled=TRUE
                LEFT JOIN sites si ON si.tenant_id=s.tenant_id
                  AND (si.id=s.shop_id OR si.id='site-'||s.shop_id OR si.counter_code=s.shop_id)
                WHERE t.active=TRUE AND lower(COALESCE(t.client_id,''))=lower(:client)
                  AND COALESCE(si.active,TRUE)=TRUE
                ORDER BY name,s.shop_id"""),{"client":client}).mappings().all()
        return {"users":[dict(row) for row in users],"sites":[dict(row) for row in sites]}

    def portal_user_for_client_site(self, client_id: str, user_id: str, shop_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row=conn.execute(text("""SELECT u.*,s.role AS site_role,t.id AS login_tenant_id,t.client_id,
                       COALESCE(si.company_code,u.company_code) AS login_company_code
                FROM tenants t
                JOIN portal_user_sites s ON s.tenant_id=t.id AND s.enabled=TRUE
                JOIN portal_users u ON u.id=s.user_id AND u.enabled=TRUE
                LEFT JOIN sites si ON si.tenant_id=t.id AND si.active=TRUE
                  AND (si.id=s.shop_id OR si.id='site-'||s.shop_id OR si.counter_code=s.shop_id)
                WHERE u.id=:user AND t.active=TRUE
                  AND lower(COALESCE(t.client_id,''))=lower(:client)
                  AND s.shop_id=:shop LIMIT 1"""),
                {"client":(client_id or "").strip(),"user":user_id,"shop":shop_id}).mappings().first()
        if not row: return None
        data=dict(row)
        data["tenant_id"]=data.pop("login_tenant_id")
        data["company_code"]=data.pop("login_company_code")
        data["shop_id"]=shop_id
        data["role"]=data.pop("site_role") or data.get("role") or "USER"
        return data

    def create_portal_session(self, session: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(text("""INSERT INTO portal_sessions(session_id,token_hash,tenant_id,company_code,shop_id,user_id,display_name,role,created_at,expires_at)
                VALUES(:session_id,:token_hash,:tenant_id,:company_code,:shop_id,:user_id,:display_name,:role,:created_at,:expires_at)"""),session)

    def portal_session_by_hash(self, token_hash: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row=conn.execute(text("SELECT * FROM portal_sessions WHERE token_hash=:token_hash"),{"token_hash":token_hash}).mappings().first()
        if not row: return None
        data=dict(row)
        for key in ("created_at","expires_at"):
            if hasattr(data.get(key),"isoformat"): data[key]=data[key].isoformat()
        return data
    def upsert_crm_person_mapping(self, mapping: dict[str, Any]) -> dict[str, Any]:
        now=self.now()
        with self._conn() as conn:
            conn.execute(text("""INSERT INTO crm_person_mappings(tenant_id,shop_id,local_person_id,crm_user_id,employee_code,break_master_id,enabled,created_at,updated_at)
                VALUES(:tenant,:shop,:local,:crm,:employee,:break,TRUE,:now,:now)
                ON CONFLICT(tenant_id,shop_id,local_person_id) DO UPDATE SET crm_user_id=EXCLUDED.crm_user_id,
                employee_code=EXCLUDED.employee_code,break_master_id=EXCLUDED.break_master_id,enabled=TRUE,updated_at=EXCLUDED.updated_at"""),
                {"tenant":mapping["tenant_id"],"shop":mapping["shop_id"],"local":mapping["local_person_id"],
                 "crm":mapping["crm_user_id"],"employee":mapping.get("employee_code"),"break":mapping.get("break_master_id"),"now":now})
        return self.crm_person_mapping(mapping["tenant_id"],mapping["shop_id"],mapping["local_person_id"])

    def crm_person_mapping(self, tenant_id: str, shop_id: str, local_person_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row=conn.execute(text("""SELECT tenant_id,shop_id,local_person_id,crm_user_id,employee_code,break_master_id,enabled,created_at,updated_at
                FROM crm_person_mappings WHERE tenant_id=:tenant AND shop_id=:shop AND local_person_id=:local AND enabled=TRUE"""),
                {"tenant":tenant_id,"shop":shop_id,"local":local_person_id}).mappings().first()
        return dict(row) if row else None

    def list_crm_person_mappings(self, tenant_id: str, shop_id: str) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows=conn.execute(text("""SELECT tenant_id,shop_id,local_person_id,crm_user_id,employee_code,break_master_id,enabled,created_at,updated_at
                FROM crm_person_mappings WHERE tenant_id=:tenant AND shop_id=:shop AND enabled=TRUE ORDER BY employee_code NULLS LAST,local_person_id"""),
                {"tenant":tenant_id,"shop":shop_id}).mappings().all()
        return [dict(row) for row in rows]



    def upsert_attendance_policy(self, tenant_id: str, shop_id: str, policy: dict[str, Any]) -> dict[str, Any]:
        now=self.now()
        params={
            "tenant":tenant_id,"shop":shop_id,
            "grace":int(policy["grace_period_minutes"]),
            "breaks":int(policy["allowed_break_minutes"]),
            "working":int(policy["total_working_minutes"]),
            "max_logoff":str(policy["max_logoff_time"]),
            "auto_logout":bool(policy["absence_auto_logout_enabled"]),
            "timezone":str(policy["timezone"]),
            "emails":json.dumps(policy.get("email_recipients") or []),
            "whatsapp":json.dumps(policy.get("whatsapp_recipients") or []),
            "updated_at":now,
        }
        with self._conn() as conn:
            conn.execute(text("""INSERT INTO attendance_policies(
                tenant_id,shop_id,grace_period_minutes,allowed_break_minutes,total_working_minutes,
                max_logoff_time,absence_auto_logout_enabled,timezone,email_recipients_json,
                whatsapp_recipients_json,updated_at)
                VALUES(:tenant,:shop,:grace,:breaks,:working,:max_logoff,:auto_logout,:timezone,
                       CAST(:emails AS JSONB),CAST(:whatsapp AS JSONB),:updated_at)
                ON CONFLICT(tenant_id,shop_id) DO UPDATE SET
                grace_period_minutes=EXCLUDED.grace_period_minutes,
                allowed_break_minutes=EXCLUDED.allowed_break_minutes,
                total_working_minutes=EXCLUDED.total_working_minutes,
                max_logoff_time=EXCLUDED.max_logoff_time,
                absence_auto_logout_enabled=EXCLUDED.absence_auto_logout_enabled,
                timezone=EXCLUDED.timezone,
                email_recipients_json=EXCLUDED.email_recipients_json,
                whatsapp_recipients_json=EXCLUDED.whatsapp_recipients_json,
                updated_at=EXCLUDED.updated_at"""),params)
        return self.attendance_policy(tenant_id,shop_id)

    def attendance_policy(self, tenant_id: str, shop_id: str) -> dict[str, Any]:
        with self._conn() as conn:
            row=conn.execute(text("""SELECT * FROM attendance_policies
                WHERE tenant_id=:tenant AND shop_id=:shop"""),{"tenant":tenant_id,"shop":shop_id}).mappings().first()
        if not row:
            return {
                "tenant_id":tenant_id,"shop_id":shop_id,"grace_period_minutes":15,
                "allowed_break_minutes":60,"total_working_minutes":480,"max_logoff_time":"21:30",
                "absence_auto_logout_enabled":True,"timezone":"Asia/Kolkata",
                "email_recipients":[],"whatsapp_recipients":[],"updated_at":None,
            }
        item=dict(row)
        item["email_recipients"]=list(item.pop("email_recipients_json") or [])
        item["whatsapp_recipients"]=list(item.pop("whatsapp_recipients_json") or [])
        return item

    def record_attendance_activity(self, item: dict[str, Any]) -> None:
        with self._conn() as conn:
            conn.execute(text("""INSERT INTO attendance_activity(
                id,tenant_id,shop_id,crm_user_id,local_person_id,activity_type,occurred_at,
                reason_code,source,camera_id,evidence_json,metadata_json,created_at)
                VALUES(:id,:tenant,:shop,:crm_user,:local_person,:activity,:occurred,:reason,
                       :source,:camera,CAST(:evidence AS JSONB),CAST(:metadata AS JSONB),:created)
                ON CONFLICT(id) DO NOTHING"""),{
                    "id":item["id"],"tenant":item["tenant_id"],"shop":item["shop_id"],
                    "crm_user":item["crm_user_id"],"local_person":item.get("local_person_id"),
                    "activity":item["activity_type"],"occurred":item["occurred_at"],
                    "reason":item.get("reason_code"),"source":item.get("source") or "CAMERA_EYE",
                    "camera":item.get("camera_id"),"evidence":json.dumps(item.get("evidence") or {}),
                    "metadata":json.dumps(item.get("metadata") or {}),"created":self.now(),
                })

    def list_attendance_activity(self, tenant_id: str, shop_id: str, crm_user_id: str,
                                 start_at: datetime, end_at: datetime) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows=conn.execute(text("""SELECT * FROM attendance_activity
                WHERE tenant_id=:tenant AND shop_id=:shop AND crm_user_id=:user
                  AND occurred_at>=:start AND occurred_at<:end
                ORDER BY occurred_at,id"""),{
                    "tenant":tenant_id,"shop":shop_id,"user":crm_user_id,
                    "start":start_at,"end":end_at,
                }).mappings().all()
        result=[]
        for row in rows:
            item=dict(row)
            item["evidence"]=item.pop("evidence_json") or {}
            item["metadata"]=item.pop("metadata_json") or {}
            result.append(item)
        return result


    def touch_attendance_presence(self, *, tenant_id: str, shop_id: str, local_person_id: str,
                                  crm_user_id: str, seen_at: datetime, camera_id: str,
                                  recognition_event_id: str, checked_in: bool | None = None) -> dict[str, Any]:
        now=self.now()
        with self._conn() as conn:
            conn.execute(text("""INSERT INTO attendance_presence(
                tenant_id,shop_id,local_person_id,crm_user_id,checked_in,on_break,last_seen_at,
                last_camera_id,last_recognition_event_id,checkout_claimed_at,updated_at)
                VALUES(:tenant,:shop,:person,:crm_user,:checked_in,FALSE,:seen,:camera,:event,NULL,:now)
                ON CONFLICT(tenant_id,shop_id,local_person_id) DO UPDATE SET
                crm_user_id=EXCLUDED.crm_user_id,
                checked_in=CASE WHEN :set_checked_in THEN :checked_in ELSE attendance_presence.checked_in END,
                last_seen_at=GREATEST(attendance_presence.last_seen_at,EXCLUDED.last_seen_at),
                last_camera_id=EXCLUDED.last_camera_id,
                last_recognition_event_id=EXCLUDED.last_recognition_event_id,
                checkout_claimed_at=NULL,updated_at=:now"""),{
                    "tenant":tenant_id,"shop":shop_id,"person":local_person_id,"crm_user":crm_user_id,
                    "checked_in":bool(checked_in),"set_checked_in":checked_in is not None,
                    "seen":seen_at,"camera":camera_id,"event":recognition_event_id,"now":now,
                })
            row=conn.execute(text("""SELECT * FROM attendance_presence
                WHERE tenant_id=:tenant AND shop_id=:shop AND local_person_id=:person"""),
                {"tenant":tenant_id,"shop":shop_id,"person":local_person_id}).mappings().one()
        return dict(row)

    def set_attendance_presence_break(self, tenant_id: str, shop_id: str, local_person_id: str, on_break: bool) -> None:
        with self._conn() as conn:
            conn.execute(text("""UPDATE attendance_presence SET on_break=:on_break,updated_at=:now
                WHERE tenant_id=:tenant AND shop_id=:shop AND local_person_id=:person"""),{
                "on_break":bool(on_break),"now":self.now(),"tenant":tenant_id,"shop":shop_id,"person":local_person_id})

    def claim_due_absence_checkouts(self, now: datetime, limit: int = 50) -> list[dict[str, Any]]:
        # SKIP LOCKED makes this safe when more than one API worker runs the evaluator.
        with self._conn() as conn:
            rows=conn.execute(text("""WITH due AS (
                    SELECT p.tenant_id,p.shop_id,p.local_person_id
                    FROM attendance_presence p
                    JOIN attendance_policies ap ON ap.tenant_id=p.tenant_id AND ap.shop_id=p.shop_id
                    WHERE p.checked_in=TRUE AND p.on_break=FALSE
                      AND ap.absence_auto_logout_enabled=TRUE
                      AND p.last_seen_at + (ap.grace_period_minutes * INTERVAL '1 minute') <= :now
                      AND (p.checkout_claimed_at IS NULL OR p.checkout_claimed_at < :retry_before)
                    ORDER BY p.last_seen_at
                    FOR UPDATE OF p SKIP LOCKED
                    LIMIT :limit
                )
                UPDATE attendance_presence p SET checkout_claimed_at=:now,updated_at=:now
                FROM due
                WHERE p.tenant_id=due.tenant_id AND p.shop_id=due.shop_id
                  AND p.local_person_id=due.local_person_id
                RETURNING p.*"""),{
                    "now":now,"retry_before":now-timedelta(minutes=5),"limit":max(1,min(200,int(limit)))
                }).mappings().all()
        return [dict(row) for row in rows]

    def complete_presence_checkout(self, tenant_id: str, shop_id: str, local_person_id: str, success: bool) -> None:
        with self._conn() as conn:
            if success:
                conn.execute(text("""UPDATE attendance_presence SET checked_in=FALSE,on_break=FALSE,
                    checkout_claimed_at=NULL,updated_at=:now
                    WHERE tenant_id=:tenant AND shop_id=:shop AND local_person_id=:person"""),{
                    "now":self.now(),"tenant":tenant_id,"shop":shop_id,"person":local_person_id})
            else:
                conn.execute(text("""UPDATE attendance_presence SET checkout_claimed_at=NULL,updated_at=:now
                    WHERE tenant_id=:tenant AND shop_id=:shop AND local_person_id=:person"""),{
                    "now":self.now(),"tenant":tenant_id,"shop":shop_id,"person":local_person_id})

    def attendance_camera_coverage_healthy(self, tenant_id: str, shop_id: str, now: datetime,
                                           heartbeat_max_age_seconds: int = 60) -> bool:
        with self._conn() as conn:
            rows=conn.execute(text("""SELECT h.received_at,h.status_json
                FROM edge_heartbeats h
                WHERE h.tenant_id=:tenant AND h.shop_id=:shop
                ORDER BY h.received_at DESC"""),{"tenant":tenant_id,"shop":shop_id}).mappings().all()
        for row in rows:
            received=row["received_at"]
            if received.tzinfo is None:
                received=received.replace(tzinfo=timezone.utc)
            if (now-received.astimezone(timezone.utc)).total_seconds()>heartbeat_max_age_seconds:
                continue
            status=row["status_json"] if isinstance(row["status_json"],dict) else json.loads(row["status_json"])
            for camera in status.get("cameras") or []:
                if str(camera.get("camera_role") or "").upper()=="ENTRANCE_EXIT" and bool(camera.get("enabled")) and bool(camera.get("online")):
                    return True
        return False
