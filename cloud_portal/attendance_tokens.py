"""Encrypted CRM face token vault utilities. Never log credentials."""
from __future__ import annotations
import base64
import json
import os
from datetime import datetime, timedelta, timezone
from cryptography.fernet import Fernet


def _cipher() -> Fernet:
    key=os.environ.get("CAMERA_EYE_TOKEN_ENCRYPTION_KEY","").strip()
    if not key:
        raise RuntimeError("CAMERA_EYE_TOKEN_ENCRYPTION_KEY must be configured")
    return Fernet(key.encode("ascii"))


def encrypt_token(token: str) -> str:
    if not token or not token.strip():
        raise ValueError("empty token")
    return _cipher().encrypt(token.strip().encode()).decode()


def decrypt_token(value: str) -> str:
    return _cipher().decrypt(value.encode()).decode()


class TokenScopeError(ValueError):
    """Encrypted token metadata does not match its tenant/user storage row."""


def encrypt_scoped_token(token: str, tenant_id: str, shop_id: str,
                         crm_user_id: str, crm_tenant_id: str | None = None) -> str:
    if not tenant_id or not shop_id or not crm_user_id:
        raise ValueError("tenant, shop and CRM user scope are required")
    envelope={"version":1,"tenant_id":tenant_id,"shop_id":shop_id,
              "crm_user_id":crm_user_id,"token":token}
    if crm_tenant_id is not None:
        envelope['crm_tenant_id']=crm_tenant_id
    return encrypt_token(json.dumps(envelope,separators=(",",":")))


def decrypt_scoped_token(value: str, tenant_id: str, shop_id: str,
                         crm_user_id: str, crm_tenant_id: str | None = None) -> str:
    try:
        envelope=json.loads(decrypt_token(value))
    except (ValueError,TypeError) as exc:
        raise TokenScopeError("cached CRM token has no valid scope envelope") from exc
    expected={"tenant_id":tenant_id,"shop_id":shop_id,"crm_user_id":crm_user_id}
    if crm_tenant_id is not None:
        expected['crm_tenant_id']=crm_tenant_id
    if (not isinstance(envelope,dict) or envelope.get("version")!=1
            or any(envelope.get(key)!=identity for key,identity in expected.items())):
        raise TokenScopeError("cached CRM token scope does not match its storage key")
    token=envelope.get("token")
    if not isinstance(token,str) or not token.strip():
        raise TokenScopeError("cached CRM token is empty")
    return token


def jwt_expiry(token: str, *, issued_at: datetime | None = None) -> datetime | None:
    """Return an unverified JWT expiry hint, or None when no valid exp is present."""
    if issued_at is not None and issued_at.tzinfo is None:
        raise ValueError("issued_at must be timezone aware")
    try:
        parts=token.removeprefix("Bearer ").split(".")
        raw=parts[1] + "=" * (-len(parts[1]) % 4)
        payload=json.loads(base64.urlsafe_b64decode(raw))
        exp=datetime.fromtimestamp(int(payload["exp"]),timezone.utc)
        return exp
    except (IndexError,KeyError,ValueError,TypeError,OverflowError):
        return None


def usable(expires_at: datetime, now: datetime | None = None) -> bool:
    now=now or datetime.now(timezone.utc)
    return expires_at>now+timedelta(minutes=5)
