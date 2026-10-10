"""Operator-approved bindings for the existing CRM integration credential.

Existing edge credentials alone do not confer authority on a different credential.
Bindings are provisioned by an operator or the authenticated shop-admin one-time
activation flow, never inferred from unverified request bodies or directories.
"""
from datetime import datetime, timezone
import re

from sqlalchemy import text


class IntegrationScopeStore:
    def _integration_execute(self, conn, sql, values=None):
        return conn.execute(text(sql) if hasattr(self, "engine") else sql, values or {})

    def initialize_integration_scopes(self):
        with self._conn() as conn:
            self._integration_execute(conn, """CREATE TABLE IF NOT EXISTS crm_integration_scopes(
                token_hash TEXT NOT NULL, tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL,
                enabled INTEGER NOT NULL, granted_by TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(token_hash,tenant_id,shop_id))""")

    def set_crm_integration_scope(self, token_hash, tenant_id, shop_id, *, granted_by, enabled=True):
        """Provision only from an operator or trusted scoped-admin activation flow."""
        if not re.fullmatch(r"[0-9a-f]{64}", token_hash or ""):
            raise ValueError("A SHA256 credential digest is required")
        if not all(str(v or "").strip() for v in (tenant_id, shop_id, granted_by)):
            raise ValueError("Exact tenant/shop and approving operator are required")
        if any(str(v) != str(v).strip() or "*" in str(v) for v in (tenant_id, shop_id)):
            raise ValueError("Wildcard or noncanonical scope grants are forbidden")
        with self._conn() as conn:
            self._integration_execute(conn, """INSERT INTO crm_integration_scopes(
                token_hash,tenant_id,shop_id,enabled,granted_by,updated_at)
                VALUES(:digest,:tenant,:shop,:enabled,:actor,:stamp)
                ON CONFLICT(token_hash,tenant_id,shop_id) DO UPDATE SET
                enabled=EXCLUDED.enabled,granted_by=EXCLUDED.granted_by,updated_at=EXCLUDED.updated_at""",
                {"digest":token_hash,"tenant":tenant_id,"shop":shop_id,"enabled":int(enabled),
                 "actor":granted_by,"stamp":datetime.now(timezone.utc).isoformat()})

    def crm_integration_scope_allowed(self, token_hash, tenant_id, shop_id):
        with self._conn() as conn:
            return self._integration_execute(conn, """SELECT 1 FROM crm_integration_scopes
                WHERE token_hash=:digest AND tenant_id=:tenant AND shop_id=:shop AND enabled=1""",
                {"digest":token_hash,"tenant":tenant_id,"shop":shop_id}).fetchone() is not None

    def crm_integration_site_allowed(self, token_hash, tenant_id, site_id):
        with self._conn() as conn:
            return self._integration_execute(conn, """SELECT 1 FROM crm_integration_scopes g
                JOIN edge_credentials e ON e.tenant_id=g.tenant_id AND e.shop_id=g.shop_id
                WHERE g.token_hash=:digest AND g.tenant_id=:tenant AND e.site_id=:site
                  AND g.enabled=1 AND e.enabled=TRUE LIMIT 1""",
                {"digest":token_hash,"tenant":tenant_id,"site":site_id}).fetchone() is not None
