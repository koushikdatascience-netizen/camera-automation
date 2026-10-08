from datetime import datetime, timedelta, timezone
import base64
import json
import pytest

from cryptography.fernet import Fernet
from cloud_portal.attendance_tokens import encrypt_token,decrypt_token,jwt_expiry,usable


def test_token_encrypt_roundtrip(monkeypatch):
    monkeypatch.setenv("CAMERA_EYE_TOKEN_ENCRYPTION_KEY",Fernet.generate_key().decode())
    ciphertext=encrypt_token("secret-value")
    assert ciphertext!="secret-value"
    assert decrypt_token(ciphertext)=="secret-value"


def test_encryption_requires_key(monkeypatch):
    monkeypatch.delenv("CAMERA_EYE_TOKEN_ENCRYPTION_KEY",raising=False)
    with pytest.raises(RuntimeError):
        encrypt_token("secret")


def test_jwt_exp_is_earlier_than_24h_contract():
    now=datetime(2026,10,8,tzinfo=timezone.utc)
    exp=int((now+timedelta(hours=2)).timestamp())
    body=base64.urlsafe_b64encode(json.dumps({"exp":exp}).encode()).decode().rstrip("=")
    token="a."+body+".c"
    assert jwt_expiry(token,issued_at=now)==now+timedelta(hours=2)
    assert not usable(now+timedelta(minutes=4),now)
    assert usable(now+timedelta(minutes=6),now)
