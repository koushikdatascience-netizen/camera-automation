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
