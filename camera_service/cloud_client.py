from __future__ import annotations

from typing import Any

import requests


class CloudSyncClient:
    def __init__(self, config):
        self.config = config

    def enabled(self) -> bool:
        return bool(self.config.enabled and self.config.base_url and self.config.api_token)

    def event_envelope(self, edge_config, event: dict[str, Any]) -> dict[str, Any]:
        scope = event.get("scope") or {}
        expected = {
            "tenant_id": edge_config.tenant_id,
            "company_code": getattr(edge_config, "company_code", None),
            "shop_id": getattr(edge_config, "shop_id", None) or edge_config.site_id,
            "edge_id": edge_config.edge_id,
        }
        for key, value in expected.items():
            if scope.get(key) is not None and str(scope.get(key)) != str(value):
                raise RuntimeError(f"Queued event {key} does not match configured edge identity")
        return {
            "schema_version": "edge.event.v1",
            "edge_id": expected["edge_id"],
            "tenant_id": expected["tenant_id"],
            "company_code": expected["company_code"],
            "shop_id": expected["shop_id"],
            "site_id": edge_config.site_id,
            "event_id": event.get("event_id"),
            "event_type": event.get("event_type"),
            "event_time": event.get("event_time"),
            "store_id": event.get("store_id"),
            "camera_id": event.get("camera_id"),
            "payload": event,
        }

    def post_event(self, edge_config, event: dict[str, Any]) -> dict[str, Any]:
        if not self.enabled():
            raise RuntimeError("cloud sync is disabled")

        payload = self.event_envelope(edge_config, event)
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
            "company_code": getattr(edge_config, "company_code", None),
            "shop_id": getattr(edge_config, "shop_id", None) or edge_config.site_id,
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


    def camera_config(self) -> dict[str, Any]:
        """Fetch camera assignments bound to this edge credential."""
        if not self.enabled():
            raise RuntimeError("cloud sync is disabled")
        response = requests.get(
            self.config.base_url.rstrip("/") + "/edge/v1/config/cameras",
            headers={"Authorization": f"Bearer {self.config.api_token}"},
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return response.json() if response.content else {"items": []}


    def edge_commands(self) -> list[dict[str, Any]]:
        if not self.enabled(): raise RuntimeError("cloud sync is disabled")
        response=requests.get(self.config.base_url.rstrip("/")+"/edge/v1/commands",
            headers={"Authorization":f"Bearer {self.config.api_token}"},timeout=self.config.timeout_seconds)
        response.raise_for_status()
        return (response.json() or {}).get("items") or []

    def complete_edge_command(self, command_id: str, result: dict[str, Any]) -> None:
        if not self.enabled(): raise RuntimeError("cloud sync is disabled")
        response=requests.post(self.config.base_url.rstrip("/")+f"/edge/v1/commands/{command_id}/result",
            json=result,headers={"Authorization":f"Bearer {self.config.api_token}"},timeout=self.config.timeout_seconds)
        response.raise_for_status()
