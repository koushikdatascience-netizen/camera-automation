from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class PortalStore:
    def __init__(self, path: str = "data/cloud_portal.db"):
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

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
                CREATE INDEX IF NOT EXISTS idx_edge_events_tenant_time ON edge_events(tenant_id, site_id, event_time);
                CREATE INDEX IF NOT EXISTS idx_edge_events_type ON edge_events(tenant_id, event_type, event_time);
                """
            )
            self._ensure_column(conn, "edge_events", "company_code", "TEXT")
            self._ensure_column(conn, "edge_events", "shop_id", "TEXT")

    @staticmethod
    def _ensure_column(conn, table: str, column: str, definition: str) -> None:
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat()

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
            conn.execute(
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
            )
        return {"ok": True, "event_id": event_id, "tenant_id": tenant_id, "site_id": site_id}

    def list_events(self, tenant_id: str, site_id: str | None = None, event_type: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        query = "SELECT * FROM edge_events WHERE tenant_id=?"
        args: list[Any] = [tenant_id]
        if site_id:
            query += " AND site_id=?"
            args.append(site_id)
        if event_type:
            query += " AND event_type=?"
            args.append(event_type)
        query += " ORDER BY event_time DESC LIMIT ?"
        args.append(max(1, min(500, int(limit))))
        with self._conn() as conn:
            rows = conn.execute(query, args).fetchall()
            return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

    def tenant_summary(self, tenant_id: str) -> dict[str, Any]:
        with self._conn() as conn:
            sites = conn.execute("SELECT COUNT(*) AS count FROM sites WHERE tenant_id=?", (tenant_id,)).fetchone()["count"]
            edges = conn.execute("SELECT COUNT(*) AS count FROM edge_machines WHERE tenant_id=?", (tenant_id,)).fetchone()["count"]
            events = conn.execute("SELECT event_type, COUNT(*) AS count FROM edge_events WHERE tenant_id=? GROUP BY event_type", (tenant_id,)).fetchall()
            return {
                "tenant_id": tenant_id,
                "sites": sites,
                "edges": edges,
                "events": {row["event_type"]: row["count"] for row in events},
            }


    def record_heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        tenant_id = str(payload["tenant_id"])
        site_id = str(payload["site_id"])
        edge_id = str(payload["edge_id"])
        now = self.now()
        with self._lock, self._conn() as conn:
            conn.execute("INSERT OR IGNORE INTO tenants(id,name,created_at) VALUES(?,?,?)", (tenant_id, tenant_id, now))
            conn.execute("INSERT OR IGNORE INTO sites(id,tenant_id,name,created_at) VALUES(?,?,?,?)", (site_id, tenant_id, site_id, now))
            conn.execute("INSERT OR REPLACE INTO edge_machines(id,tenant_id,site_id,last_seen_at) VALUES(?,?,?,?)", (edge_id, tenant_id, site_id, now))
            conn.execute("INSERT OR REPLACE INTO edge_heartbeats(tenant_id,site_id,edge_id,received_at,status_json) VALUES(?,?,?,?,?)", (tenant_id, site_id, edge_id, now, json.dumps(payload.get("status") or {})))
        return {"ok": True, "tenant_id": tenant_id, "site_id": site_id, "edge_id": edge_id, "received_at": now}
