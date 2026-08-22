from __future__ import annotations

from typing import Any

import requests


class CloudSyncClient:
    def __init__(self, config):
        self.config = config

    def enabled(self) -> bool:
        return bool(self.config.enabled and self.config.base_url and self.config.api_token)

    def post_event(self, edge_config, event: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled():
            raise RuntimeError("cloud sync is disabled")

        payload = {
            "edge_id": edge_config.edge_id,
            "tenant_id": edge_config.tenant_id,
            "site_id": edge_config.site_id,
            "event": event,
        }
        response = requests.post(
            self.config.base_url.rstrip("/") + "/edge/v1/events",
            json=payload,
            headers={"Authorization": f"Bearer {self.config.api_token}"},
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return response.json() if response.content else {"ok": True}

    def heartbeat(self, edge_config, status: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled():
            raise RuntimeError("cloud sync is disabled")

        payload = {
            "edge_id": edge_config.edge_id,
            "tenant_id": edge_config.tenant_id,
            "site_id": edge_config.site_id,
            "status": status,
        }
        response = requests.post(
            self.config.base_url.rstrip("/") + "/edge/v1/heartbeat",
            json=payload,
            headers={"Authorization": f"Bearer {self.config.api_token}"},
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return response.json() if response.content else {"ok": True}
