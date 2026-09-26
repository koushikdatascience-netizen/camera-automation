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
            """CREATE TABLE IF NOT EXISTS sites(id TEXT NOT NULL, tenant_id TEXT NOT NULL, name TEXT, created_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(id,tenant_id))""",
            """CREATE TABLE IF NOT EXISTS edge_machines(id TEXT NOT NULL, tenant_id TEXT NOT NULL, site_id TEXT NOT NULL, last_seen_at TIMESTAMPTZ NOT NULL, PRIMARY KEY(id,tenant_id,site_id))""",
            """CREATE TABLE IF NOT EXISTS edge_heartbeats(tenant_id TEXT NOT NULL, site_id TEXT NOT NULL, edge_id TEXT NOT NULL, received_at TIMESTAMPTZ NOT NULL, status_json JSONB NOT NULL, PRIMARY KEY(tenant_id,site_id,edge_id))""",
            """CREATE TABLE IF NOT EXISTS edge_credentials(token_hash TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, company_code TEXT, shop_id TEXT NOT NULL, site_id TEXT NOT NULL, edge_id TEXT NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE, created_at TIMESTAMPTZ NOT NULL)""",
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
        ]
        with self._conn() as conn:
            for statement in statements:
                conn.execute(text(statement))

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    def resolve_edge_credential(self, token_hash: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute(text("""SELECT tenant_id,company_code,shop_id,site_id,edge_id
                FROM edge_credentials WHERE token_hash=:token_hash AND enabled=TRUE"""),
                {"token_hash": token_hash}).mappings().first()
        return dict(row) if row else None

    def provision_edge_credential(self, token_hash: str, tenant_id: str, company_code: str | None,
                                  shop_id: str, site_id: str, edge_id: str) -> None:
        with self._conn() as conn:
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

    def ingest_event(self, envelope: dict[str, Any]) -> dict[str, Any]:
        tenant_id=str(envelope["tenant_id"]); site_id=str(envelope["site_id"]); edge_id=str(envelope["edge_id"]); event_id=str(envelope["event_id"]); now=self.now()
        with self._conn() as conn:
            conn.execute(text("INSERT INTO tenants(id,name,created_at) VALUES(:id,:name,:now) ON CONFLICT(id) DO NOTHING"),{"id":tenant_id,"name":tenant_id,"now":now})
            conn.execute(text("INSERT INTO sites(id,tenant_id,name,created_at) VALUES(:id,:tenant,:name,:now) ON CONFLICT(id,tenant_id) DO NOTHING"),{"id":site_id,"tenant":tenant_id,"name":site_id,"now":now})
            conn.execute(text("INSERT INTO edge_machines(id,tenant_id,site_id,last_seen_at) VALUES(:id,:tenant,:site,:now) ON CONFLICT(id,tenant_id,site_id) DO UPDATE SET last_seen_at=EXCLUDED.last_seen_at"),{"id":edge_id,"tenant":tenant_id,"site":site_id,"now":now})
            conn.execute(text("""INSERT INTO edge_events(id,tenant_id,company_code,shop_id,site_id,edge_id,store_id,camera_id,event_type,event_time,received_at,payload_json)
                VALUES(:id,:tenant,:company,:shop,:site,:edge,:store,:camera,:type,:event_time,:received,CAST(:payload AS JSONB))
                ON CONFLICT(id) DO NOTHING"""),{
                "id":event_id,"tenant":tenant_id,"company":envelope.get("company_code"),"shop":envelope.get("shop_id") or site_id,
                "site":site_id,"edge":edge_id,"store":envelope.get("store_id"),"camera":envelope.get("camera_id"),"type":envelope.get("event_type"),
                "event_time":envelope.get("event_time"),"received":now,"payload":json.dumps(envelope)})
        return {"ok":True,"event_id":event_id,"tenant_id":tenant_id,"site_id":site_id}

    def list_events(self, tenant_id: str, site_id: str | None = None, event_type: str | None = None, limit: int = 100):
        clauses=["tenant_id=:tenant"]; params={"tenant":tenant_id,"limit":max(1,min(500,int(limit)))}
        if site_id: clauses.append("site_id=:site"); params["site"]=site_id
        if event_type: clauses.append("event_type=:event_type"); params["event_type"]=event_type
        query="SELECT * FROM edge_events WHERE "+" AND ".join(clauses)+" ORDER BY event_time DESC LIMIT :limit"
        with self._conn() as conn:
            rows=conn.execute(text(query),params).mappings().all()
            return [dict(r) | {"payload": r["payload_json"] if isinstance(r["payload_json"],dict) else json.loads(r["payload_json"])} for r in rows]

    def tenant_summary(self, tenant_id: str):
        with self._conn() as conn:
            sites=conn.execute(text("SELECT COUNT(*) FROM sites WHERE tenant_id=:t"),{"t":tenant_id}).scalar_one()
            edges=conn.execute(text("SELECT COUNT(*) FROM edge_machines WHERE tenant_id=:t"),{"t":tenant_id}).scalar_one()
            events=conn.execute(text("SELECT event_type,COUNT(*) AS count FROM edge_events WHERE tenant_id=:t GROUP BY event_type"),{"t":tenant_id}).mappings().all()
        return {"tenant_id":tenant_id,"sites":sites,"edges":edges,"events":{r["event_type"]:r["count"] for r in events}}

    def record_heartbeat(self, payload: dict[str, Any]):
        tenant=str(payload["tenant_id"]); site=str(payload["site_id"]); edge=str(payload["edge_id"]); now=self.now()
        with self._conn() as conn:
            conn.execute(text("INSERT INTO tenants(id,name,created_at) VALUES(:id,:id,:now) ON CONFLICT(id) DO NOTHING"),{"id":tenant,"now":now})
            conn.execute(text("INSERT INTO sites(id,tenant_id,name,created_at) VALUES(:site,:tenant,:site,:now) ON CONFLICT(id,tenant_id) DO NOTHING"),{"site":site,"tenant":tenant,"now":now})
            conn.execute(text("INSERT INTO edge_machines(id,tenant_id,site_id,last_seen_at) VALUES(:edge,:tenant,:site,:now) ON CONFLICT(id,tenant_id,site_id) DO UPDATE SET last_seen_at=EXCLUDED.last_seen_at"),{"edge":edge,"tenant":tenant,"site":site,"now":now})
            conn.execute(text("""INSERT INTO edge_heartbeats(tenant_id,site_id,edge_id,received_at,status_json)
                VALUES(:tenant,:site,:edge,:now,CAST(:status AS JSONB))
                ON CONFLICT(tenant_id,site_id,edge_id) DO UPDATE SET received_at=EXCLUDED.received_at,status_json=EXCLUDED.status_json"""),
                {"tenant":tenant,"site":site,"edge":edge,"now":now,"status":json.dumps(payload.get("status") or {})})
        return {"ok":True,"tenant_id":tenant,"site_id":site,"edge_id":edge,"received_at":now.isoformat()}


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

    def get_edge_command(self, command_id: str, tenant_id: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row=conn.execute(text("""SELECT id,tenant_id,shop_id,edge_id,command_type,status,result_json,created_at,claimed_at,completed_at
                FROM edge_commands WHERE id=:id AND tenant_id=:tenant"""),{"id":command_id,"tenant":tenant_id}).mappings().first()
        if not row: return None
        data=dict(row)
        for key in ("created_at","claimed_at","completed_at"):
            if hasattr(data.get(key),"isoformat"): data[key]=data[key].isoformat()
        data["result"]=data.pop("result_json")
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
