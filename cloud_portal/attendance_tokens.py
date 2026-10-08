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


def jwt_expiry(token: str, *, issued_at: datetime | None = None) -> datetime:
    """Bound 24h CRM contract by embedded exp; decoding is NOT signature verification."""
    now=issued_at or datetime.now(timezone.utc)
    if now.tzinfo is None:
        raise ValueError("issued_at must be timezone aware")
    max_exp=now+timedelta(hours=24)
    try:
        parts=token.removeprefix("Bearer ").split(".")
        raw=parts[1] + "=" * (-len(parts[1]) % 4)
        payload=json.loads(base64.urlsafe_b64decode(raw))
        exp=datetime.fromtimestamp(int(payload["exp"]),timezone.utc)
        return min(max_exp,exp)
    except (IndexError,KeyError,ValueError,TypeError,OverflowError):
        return max_exp


def usable(expires_at: datetime, now: datetime | None = None) -> bool:
    now=now or datetime.now(timezone.utc)
    return expires_at>now+timedelta(minutes=5)
