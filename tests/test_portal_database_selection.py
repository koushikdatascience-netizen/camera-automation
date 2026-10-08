from types import SimpleNamespace

import pytest

from cloud_portal.api import build_portal_store
from cloud_portal.storage import PortalStore


def test_development_portal_can_use_sqlite(monkeypatch, tmp_path):
    monkeypatch.delenv("SNAPKEY_DATABASE_URL", raising=False)
    monkeypatch.setenv("SNAPKEY_ENV", "development")
    monkeypatch.setenv("SNAPKEY_PORTAL_DB", str(tmp_path / "portal.db"))
    assert isinstance(build_portal_store(), PortalStore)


def test_production_requires_postgres(monkeypatch):
    monkeypatch.delenv("SNAPKEY_DATABASE_URL", raising=False)
    monkeypatch.setenv("SNAPKEY_ENV", "production")
    with pytest.raises(RuntimeError, match="SNAPKEY_DATABASE_URL"):
        build_portal_store()
