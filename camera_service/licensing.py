from __future__ import annotations

import hashlib
import os
import platform
import socket
import uuid
from dataclasses import dataclass

from camera_service.config import EdgeConfig


@dataclass(frozen=True)
class LicenseStatus:
    active: bool
    mode: str
    plan: str
    machine_code: str
    tenant_id: str
    site_id: str
    reason: str

    def model_dump(self) -> dict:
        return {
            "active": self.active,
            "mode": self.mode,
            "plan": self.plan,
            "machine_code": self.machine_code,
            "tenant_id": self.tenant_id,
            "site_id": self.site_id,
            "reason": self.reason,
        }


class LicenseManager:
    """Local commercial gate for cloud features; real entitlement lives on the portal."""

    def __init__(self, edge: EdgeConfig):
        self.edge = edge

    def machine_code(self) -> str:
        raw = "|".join(
            [
                socket.gethostname(),
                platform.system(),
                platform.machine(),
                str(uuid.getnode()),
            ]
        )
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest().upper()
        return "-".join([digest[0:6], digest[6:12], digest[12:18], digest[18:24]])

    def status(self) -> LicenseStatus:
        override = os.getenv("CAMERA_AUTOMATION_ACTIVATION_TOKEN", "")
        token = (override or self.edge.activation_token or "").strip()

        if not self.edge.activation_required:
            return LicenseStatus(
                active=True,
                mode="demo-local",
                plan=self.edge.plan,
                machine_code=self.machine_code(),
                tenant_id=self.edge.tenant_id,
                site_id=self.edge.site_id,
                reason="Activation is not required for this local/demo configuration.",
            )

        if token:
            return LicenseStatus(
                active=True,
                mode="activated",
                plan=self.edge.plan,
                machine_code=self.machine_code(),
                tenant_id=self.edge.tenant_id,
                site_id=self.edge.site_id,
                reason="Activation token is configured.",
            )

        return LicenseStatus(
            active=False,
            mode="activation-required",
            plan=self.edge.plan,
            machine_code=self.machine_code(),
            tenant_id=self.edge.tenant_id,
            site_id=self.edge.site_id,
            reason="Cloud sync and paid alert delivery require activation from the platform.",
        )
