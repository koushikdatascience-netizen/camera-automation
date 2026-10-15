from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def heartbeat_payload(edge_id: str, tenant_id: str, site_id: str, status: dict[str, Any]) -> dict[str, Any]:
    return {
        "edge_id": edge_id,
        "tenant_id": tenant_id,
        "site_id": site_id,
        "received_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
    }
