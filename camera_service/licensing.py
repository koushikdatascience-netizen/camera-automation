from __future__ import annotations

import hashlib
import base64
import json
import os
import platform
import socket
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat

from camera_service.config import EdgeConfig


ALL_FEATURES = {
    "tracking",
    "attendance",
    "face_recognition",
    "unknown_detection",
    "unknown_person_detection",
    "shoplifting",
    "shoplifting_detection",
    "object_security",
    "cloud_sync",
    "alerts",
    "evidence_clips",
}


def _canonical_json(data: dict[str, Any]) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def generate_license_keypair() -> dict[str, str]:
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    return {
        "private_key": base64.b64encode(
            private_key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        ).decode("ascii"),
        "public_key": base64.b64encode(public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)).decode("ascii"),
    }


def sign_license_payload(payload: dict[str, Any], private_key_b64: str) -> str:
    private_key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(private_key_b64))
    return base64.b64encode(private_key.sign(_canonical_json(payload))).decode("ascii")


def verify_license_signature(payload: dict[str, Any], signature_b64: str, public_key_b64: str) -> None:
    public_key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
    try:
        public_key.verify(base64.b64decode(signature_b64), _canonical_json(payload))
    except InvalidSignature as exc:
        raise ValueError("signature mismatch") from exc


@dataclass(frozen=True)
class LicenseStatus:
    active: bool
    mode: str
    plan: str
    machine_code: str
    tenant_id: str
    site_id: str
    reason: str
    limited_mode: bool = False
    max_cameras: int = 1
    features: list[str] = field(default_factory=list)
    expires_at: str | None = None
    grace_until: str | None = None
    source: str = "config"

    def allows_feature(self, feature: str) -> bool:
        normalized = feature.strip().lower()
        return "*" in self.features or normalized in self.features

    def model_dump(self) -> dict:
        return {
            "active": self.active,
            "mode": self.mode,
            "plan": self.plan,
            "machine_code": self.machine_code,
            "tenant_id": self.tenant_id,
            "site_id": self.site_id,
            "reason": self.reason,
            "limited_mode": self.limited_mode,
            "max_cameras": self.max_cameras,
            "features": self.features,
            "expires_at": self.expires_at,
            "grace_until": self.grace_until,
            "source": self.source,
        }


class LicenseManager:
    """Local commercial gate; server-issued signed license is cached for offline grace."""

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

    def public_key(self) -> str:
        return (os.getenv("SNAPKEY_LICENSE_PUBLIC_KEY") or self.edge.license_public_key or "").strip()

    def cache_path(self) -> Path:
        return Path(self.edge.license_cache_path)

    def install_signed_license(self, license_payload: dict[str, Any], signature: str) -> LicenseStatus:
        self._validate_signed_license(license_payload, signature)
        path = self.cache_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"license": license_payload, "signature": signature}, indent=2), encoding="utf-8")
        return self.status()

    def status(self) -> LicenseStatus:
        if not self.edge.activation_required:
            return LicenseStatus(
                active=True,
                mode="demo-local",
                plan=self.edge.plan,
                machine_code=self.machine_code(),
                tenant_id=self.edge.tenant_id,
                site_id=self.edge.site_id,
                reason="Activation is not required for this local/demo configuration.",
                limited_mode=False,
                max_cameras=999,
                features=sorted(ALL_FEATURES),
                source="config",
            )

        cached = self._read_cached_license()
        if cached:
            payload, signature = cached
            try:
                self._validate_signed_license(payload, signature)
                return self._status_from_payload(payload)
            except Exception as exc:
                return self._limited(f"Cached license is invalid: {exc}")

        return self._limited("No signed license cache is installed. Activate this edge from the portal.")

    def _read_cached_license(self) -> tuple[dict[str, Any], str] | None:
        path = self.cache_path()
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("license") or {}, str(data.get("signature") or "")

    def _validate_signed_license(self, payload: dict[str, Any], signature: str) -> None:
        public_key = self.public_key()
        if not public_key:
            raise ValueError("license public key is not configured")
        verify_license_signature(payload, signature, public_key)
        if payload.get("machine_code") and payload["machine_code"] != self.machine_code():
            raise ValueError("license is for a different machine")
        if payload.get("tenant_id") != self.edge.tenant_id or payload.get("site_id") != self.edge.site_id:
            raise ValueError("license tenant/site does not match this edge")
        if payload.get("edge_id") != self.edge.edge_id:
            raise ValueError("license edge_id does not match this machine")

    def _status_from_payload(self, payload: dict[str, Any]) -> LicenseStatus:
        now = datetime.now(timezone.utc)
        expires_at = str(payload.get("expires_at") or "")
        grace_until = str(payload.get("grace_until") or expires_at)
        expiry = self._parse_time(expires_at)
        grace = self._parse_time(grace_until)
        active = bool(expiry and now <= expiry)
        in_grace = bool(not active and grace and now <= grace)
        enabled = active or in_grace
        mode = "activated" if active else "grace" if in_grace else "expired"
        reason = "License is active." if active else "License expired but grace period is active." if in_grace else "License and grace period have expired."
        features = [str(item).strip().lower() for item in payload.get("features", []) if str(item).strip()]
        return LicenseStatus(
            active=enabled,
            mode=mode,
            plan=str(payload.get("plan") or self.edge.plan),
            machine_code=self.machine_code(),
            tenant_id=self.edge.tenant_id,
            site_id=self.edge.site_id,
            reason=reason,
            limited_mode=not enabled,
            max_cameras=max(1, int(payload.get("max_cameras") or 1)),
            features=features or ["tracking"],
            expires_at=expires_at or None,
            grace_until=grace_until or None,
            source="signed-cache",
        )

    def _limited(self, reason: str) -> LicenseStatus:
        return LicenseStatus(
            active=False,
            mode="limited",
            plan="limited",
            machine_code=self.machine_code(),
            tenant_id=self.edge.tenant_id,
            site_id=self.edge.site_id,
            reason=reason,
            limited_mode=True,
            max_cameras=1,
            features=["tracking"],
            source="limited",
        )

    @staticmethod
    def _parse_time(value: str) -> datetime | None:
        if not value:
            return None
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
