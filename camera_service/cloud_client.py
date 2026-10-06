from __future__ import annotations

from typing import Any
from pathlib import Path

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

    def upload_event_evidence(self, event_id: str, evidence_path: str) -> dict[str, Any]:
        if not self.enabled():
            raise RuntimeError("cloud sync is disabled")
        path = Path(evidence_path)
        if not path.is_file():
            raise FileNotFoundError(f"event evidence not found: {path}")
        suffix=path.suffix.lower()
        content_type={".jpg":"image/jpeg",".jpeg":"image/jpeg",".png":"image/png",".webp":"image/webp",".mp4":"video/mp4"}.get(suffix,"application/octet-stream")
        with path.open("rb") as stream:
            response = requests.post(
                self.config.base_url.rstrip("/") + f"/edge/v1/events/{event_id}/evidence",
                files={"file": (path.name, stream, content_type)},
                headers={"Authorization": f"Bearer {self.config.api_token}"},
                timeout=max(float(self.config.timeout_seconds), 60.0 if content_type=="video/mp4" else 30.0),
            )
        response.raise_for_status()
        return response.json()

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


    def personnel_config(self) -> dict[str, Any]:
        """Fetch the tenant/shop-scoped personnel roster and face embeddings."""
        if not self.enabled():
            raise RuntimeError("cloud sync is disabled")
        response=requests.get(
            self.config.base_url.rstrip("/")+"/edge/v1/config/personnel",
            headers={"Authorization":f"Bearer {self.config.api_token}"},
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return response.json() if response.content else {"items":[]}

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

    def latest_update(self, current_build_id: str) -> dict[str, Any]:
        if not self.enabled():
            raise RuntimeError("cloud sync is disabled")
        response=requests.get(self.config.base_url.rstrip("/")+"/edge/v1/updates/latest",
            params={"current_build_id":current_build_id},headers={"Authorization":f"Bearer {self.config.api_token}"},
            timeout=self.config.timeout_seconds)
        response.raise_for_status()
        return response.json()

    def download_update(self, build_id: str, destination: Path) -> None:
        if not self.enabled():
            raise RuntimeError("cloud sync is disabled")
        response=requests.get(self.config.base_url.rstrip("/")+f"/edge/v1/updates/{build_id}/download",
            headers={"Authorization":f"Bearer {self.config.api_token}"},timeout=max(120.0,float(self.config.timeout_seconds)),stream=True)
        response.raise_for_status()
        destination.parent.mkdir(parents=True,exist_ok=True)
        temporary=destination.with_suffix(destination.suffix+".part")
        with temporary.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024*1024):
                if chunk: output.write(chunk)
        temporary.replace(destination)

    def model_manifest(self) -> dict[str, Any]:
        if not self.enabled():
            raise RuntimeError("cloud sync is disabled")
        response = requests.get(
            self.config.base_url.rstrip("/") + "/edge/v1/models/manifest",
            headers={"Authorization": f"Bearer {self.config.api_token}"},
            timeout=self.config.timeout_seconds,
        )
        response.raise_for_status()
        return response.json()

    def download_model(self, model_id: str, version: str, destination: Path) -> None:
        if not self.enabled():
            raise RuntimeError("cloud sync is disabled")
        response = requests.get(
            self.config.base_url.rstrip("/") + f"/edge/v1/models/{model_id}/{version}/download",
            headers={"Authorization": f"Bearer {self.config.api_token}"},
            timeout=max(300.0, float(self.config.timeout_seconds)),
            stream=True,
        )
        response.raise_for_status()
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("wb") as output:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    output.write(chunk)
