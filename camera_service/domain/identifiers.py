from __future__ import annotations

from pydantic import BaseModel, Field


class ResourceScope(BaseModel):
    """Canonical tenant/shop/device/camera scope carried across Vision boundaries."""

    tenant_id: str = Field(min_length=1)
    company_code: str | None = None
    shop_id: str = Field(min_length=1)
    edge_id: str = Field(min_length=1)
    camera_id: str = Field(min_length=1)
