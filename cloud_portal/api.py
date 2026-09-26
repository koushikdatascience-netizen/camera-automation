from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from dataclasses import dataclass
import hashlib
import hmac
import secrets
from urllib.parse import quote

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field

from camera_service.licensing import sign_license_payload
from cloud_portal.storage import PortalStore
from cloud_portal.postgres_storage import PostgresPortalStore


def build_portal_store():
    database_url = os.getenv("SNAPKEY_DATABASE_URL", "").strip()
    if database_url:
        return PostgresPortalStore(database_url)
    if os.getenv("SNAPKEY_ENV", "development").strip().lower() == "production":
        raise RuntimeError("SNAPKEY_DATABASE_URL is required in production")
    return PortalStore(os.getenv("SNAPKEY_PORTAL_DB", "data/cloud_portal.db"))


store = build_portal_store()
app = FastAPI(title="SnapKey Vision AI Portal")
if os.getenv("SNAPKEY_ENABLE_CLOUD_INFERENCE", "0").strip() == "1":
    from cloud_portal.inference_api import router as inference_router
    app.include_router(inference_router)


class LicenseIssueRequest(BaseModel):
    tenant_id: str
    site_id: str
    edge_id: str
    machine_code: str
    plan: str = "professional"
    max_cameras: int = 7
    features: list[str] = Field(default_factory=lambda: ["tracking", "attendance", "face_recognition", "unknown_detection", "shoplifting", "object_security", "cloud_sync", "alerts", "evidence_clips"])
    days: int = 30
    grace_days: int = 7


@dataclass(frozen=True)
class EdgePrincipal:
    tenant_id: str | None = None
    company_code: str | None = None
    shop_id: str | None = None
    site_id: str | None = None
    edge_id: str | None = None
    legacy_global: bool = False


def _production() -> bool:
    return os.getenv("SNAPKEY_ENV", "development").strip().lower() == "production"


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def require_edge_token(authorization: str | None = Header(default=None)) -> EdgePrincipal:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Missing edge API token")
    token = authorization[7:].strip()
    principal = store.resolve_edge_credential(_token_digest(token))
    if principal:
        return EdgePrincipal(**principal)
    legacy = os.getenv("SNAPKEY_EDGE_API_TOKEN", "").strip()
    if legacy and hmac.compare_digest(token, legacy):
        if _production() and os.getenv("SNAPKEY_ALLOW_LEGACY_GLOBAL_EDGE_TOKEN", "0").strip() != "1":
            raise HTTPException(401, "Legacy global edge token is disabled in production")
        return EdgePrincipal(legacy_global=True)
    raise HTTPException(401, "Invalid edge API token")


def _enforce_edge_scope(principal: EdgePrincipal, payload: dict[str, Any]) -> None:
    if principal.legacy_global:
        return
    for key, expected in {
        "tenant_id": principal.tenant_id,
        "company_code": principal.company_code,
        "shop_id": principal.shop_id,
        "site_id": principal.site_id,
        "edge_id": principal.edge_id,
    }.items():
        if expected is not None and str(payload.get(key) or "") != str(expected):
            raise HTTPException(403, f"Edge credential is not authorized for {key}")


@dataclass(frozen=True)
class PortalPrincipal:
    session_id: str
    tenant_id: str
    company_code: str | None
    shop_id: str
    user_id: str | None
    display_name: str | None
    role: str


class CrmPortalSessionRequest(BaseModel):
    tenantId: str | None = None
    companyCode: str | None = None
    shopCode: str
    userId: str | None = None
    displayName: str | None = None
    role: str = "USER"


def _crm_integration_key_valid(value: str | None) -> bool:
    expected=os.getenv("SNAPKEY_CRM_INTEGRATION_KEY","").strip()
    return bool(expected and value and secrets.compare_digest(value.strip(),expected))


@app.post("/crm/session")
def create_crm_portal_session(payload: CrmPortalSessionRequest, request: Request):
    supplied=request.headers.get("X-CRM-Integration-Key","")
    if not _crm_integration_key_valid(supplied):
        raise HTTPException(401,"Invalid CRM integration key")
    shop=payload.shopCode.strip()
    if not shop: raise HTTPException(400,"shopCode is required")
    tenant=(payload.tenantId or payload.companyCode or "").strip()
    if not tenant: raise HTTPException(400,"tenantId or companyCode is required")
    session_id=secrets.token_urlsafe(18)
    token=secrets.token_urlsafe(32)
    now=datetime.now(timezone.utc)
    ttl=max(5,min(240,int(os.getenv("SNAPKEY_PORTAL_SESSION_TTL_MINUTES","60"))))
    expires=now+timedelta(minutes=ttl)
    store.create_portal_session({
        "session_id":session_id,"token_hash":_token_digest(token),"tenant_id":tenant,
        "company_code":(payload.companyCode or "").strip() or None,"shop_id":shop,
        "user_id":(payload.userId or "").strip() or None,"display_name":(payload.displayName or "").strip() or None,
        "role":(payload.role or "USER").strip().upper(),"created_at":now.isoformat(),"expires_at":expires.isoformat(),
    })
    base=os.getenv("SNAPKEY_PUBLIC_BASE_URL","").rstrip("/")
    launch=(base or "")+"/portal?sessionId="+quote(session_id)+"#session="+quote(token)
    return {"sessionId":session_id,"expiresAt":expires.isoformat(),"launchUrl":launch}


def require_portal_session(authorization: str | None = Header(default=None)) -> PortalPrincipal:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401,"Missing portal session")
    token=authorization[7:].strip()
    session=store.portal_session_by_hash(_token_digest(token))
    if not session: raise HTTPException(401,"Invalid portal session")
    if datetime.fromisoformat(str(session["expires_at"])) < datetime.now(timezone.utc):
        raise HTTPException(401,"Portal session expired")
    return PortalPrincipal(session_id=session["session_id"],tenant_id=session["tenant_id"],
        company_code=session.get("company_code"),shop_id=session["shop_id"],user_id=session.get("user_id"),
        display_name=session.get("display_name"),role=session["role"])


@app.get("/session/status")
def portal_session_status(principal: PortalPrincipal = Depends(require_portal_session)):
    return {"sessionId":principal.session_id,"tenantId":principal.tenant_id,"companyCode":principal.company_code,
        "shopCode":principal.shop_id,"userId":principal.user_id,"displayName":principal.display_name,"role":principal.role}


@app.get("/health")
def health():
    return {"status": "ok", "service": "snapkey-portal"}


PORTAL_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=PORTAL_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
@app.get("/portal", include_in_schema=False)
def portal_home():
    return FileResponse(PORTAL_STATIC_DIR / "index.html")


@app.get("/portal/{page_name}", include_in_schema=False)
def portal_page(page_name: str):
    allowed = {"index.html", "cameras.html", "personnel.html", "attendance.html"}
    if page_name not in allowed:
        raise HTTPException(404, "Portal page not found")
    return FileResponse(PORTAL_STATIC_DIR / page_name)

class PortalCameraConfig(BaseModel):
    tenant_id: str
    company_code: str | None = None
    shop_id: str
    site_id: str
    edge_id: str
    camera_id: str
    name: str
    source_type: str = "rtsp"
    source: str
    camera_role: str = "GENERAL"
    camera_zone: str | None = None
    crowd_threshold: int = 10
    enabled: bool = True
    features: dict[str, bool] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)


@app.get("/portal/v1/tenants/{tenant_id}/cameras")
def portal_cameras(tenant_id: str, shop_id: str | None = None, edge_id: str | None = None):
    return {"items": store.list_cameras(tenant_id, shop_id=shop_id, edge_id=edge_id)}


@app.put("/portal/v1/tenants/{tenant_id}/cameras/{camera_id}")
def save_portal_camera(tenant_id: str, camera_id: str, request: PortalCameraConfig):
    if request.tenant_id != tenant_id or request.camera_id != camera_id:
        raise HTTPException(400, "Camera scope does not match request path")
    if request.source_type not in {"rtsp", "file", "webcam"}:
        raise HTTPException(400, "Unsupported camera source type")
    if not request.source.strip():
        raise HTTPException(400, "Camera source is required")
    return {"camera": store.upsert_camera(request.model_dump())}


@app.delete("/portal/v1/tenants/{tenant_id}/cameras/{camera_id}")
def delete_portal_camera(tenant_id: str, camera_id: str, shop_id: str, edge_id: str):
    if not store.delete_camera(tenant_id, shop_id, edge_id, camera_id):
        raise HTTPException(404, "Camera not found")
    return {"deleted": True}


class EdgeCommandRequest(BaseModel):
    tenant_id: str
    shop_id: str
    edge_id: str
    command_type: str
    request: dict[str, Any] = Field(default_factory=dict)


@app.post("/portal/v1/tenants/{tenant_id}/edge-commands")
def create_portal_edge_command(tenant_id: str, request: EdgeCommandRequest):
    if request.tenant_id != tenant_id:
        raise HTTPException(400, "Command tenant does not match request path")
    if request.command_type not in {"ONVIF_PROBE", "CAMERA_TEST"}:
        raise HTTPException(400, "Unsupported edge command")
    return store.create_edge_command(request.model_dump())


@app.get("/portal/v1/tenants/{tenant_id}/edge-commands/{command_id}")
def portal_edge_command(tenant_id: str, command_id: str):
    command=store.get_edge_command(command_id, tenant_id)
    if not command: raise HTTPException(404, "Edge command not found")
    return command


@app.get("/edge/v1/commands")
def edge_commands(principal: EdgePrincipal = Depends(require_edge_token)):
    if principal.legacy_global:
        raise HTTPException(403, "Scoped edge credential is required for commands")
    return {"items":store.claim_edge_commands(principal.tenant_id,principal.shop_id,principal.edge_id)}


@app.post("/edge/v1/commands/{command_id}/result")
def edge_command_result(command_id: str, result: dict[str, Any], principal: EdgePrincipal = Depends(require_edge_token)):
    if principal.legacy_global:
        raise HTTPException(403, "Scoped edge credential is required for commands")
    status="SUCCEEDED" if result.get("ok",False) else "FAILED"
    if not store.complete_edge_command(command_id,principal.tenant_id,principal.shop_id,principal.edge_id,status,result):
        raise HTTPException(404, "Claimed edge command not found")
    return {"ok":True}


@app.get("/edge/v1/config/cameras")
def edge_camera_config(principal: EdgePrincipal = Depends(require_edge_token)):
    if principal.legacy_global:
        raise HTTPException(403, "Scoped edge credential is required for camera configuration")
    return {
        "tenant_id": principal.tenant_id,
        "company_code": principal.company_code,
        "shop_id": principal.shop_id,
        "site_id": principal.site_id,
        "edge_id": principal.edge_id,
        "items": store.list_cameras(principal.tenant_id, shop_id=principal.shop_id, edge_id=principal.edge_id),
    }


@app.post("/edge/v1/events")
def ingest_edge_event(envelope: dict[str, Any], principal: EdgePrincipal = Depends(require_edge_token)):
    required = ["schema_version", "tenant_id", "site_id", "edge_id", "event_id", "event_type", "event_time", "payload"]
    missing = [key for key in required if not envelope.get(key)]
    if missing:
        raise HTTPException(400, {"missing": missing})
    if envelope["schema_version"] != "edge.event.v1":
        raise HTTPException(400, "Unsupported event schema")
    _enforce_edge_scope(principal, envelope)
    return store.ingest_event(envelope)


@app.get("/portal/v1/tenants/{tenant_id}/summary")
def tenant_summary(tenant_id: str):
    return store.tenant_summary(tenant_id)


@app.get("/portal/v1/tenants/{tenant_id}/events")
def tenant_events(tenant_id: str, site_id: str | None = None, event_type: str | None = None, limit: int = 100):
    return {"items": store.list_events(tenant_id, site_id=site_id, event_type=event_type, limit=limit)}


@app.post("/portal/v1/licenses/issue")
def issue_license(request: LicenseIssueRequest):
    private_key = os.getenv("SNAPKEY_LICENSE_PRIVATE_KEY", "").strip()
    if not private_key:
        raise HTTPException(500, "SNAPKEY_LICENSE_PRIVATE_KEY is required to issue licenses")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=max(1, request.days))
    grace = expires + timedelta(days=max(0, request.grace_days))
    payload = {
        "tenant_id": request.tenant_id,
        "site_id": request.site_id,
        "edge_id": request.edge_id,
        "machine_code": request.machine_code,
        "plan": request.plan,
        "max_cameras": max(1, request.max_cameras),
        "features": sorted({feature.strip().lower() for feature in request.features if feature.strip()}),
        "issued_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "grace_until": grace.isoformat(),
    }
    return {"license": payload, "signature": sign_license_payload(payload, private_key)}


@app.post("/edge/v1/heartbeat")
def edge_heartbeat(payload: dict[str, Any], principal: EdgePrincipal = Depends(require_edge_token)):
    required = ["tenant_id", "site_id", "edge_id", "status"]
    missing = [key for key in required if payload.get(key) is None]
    if missing:
        raise HTTPException(400, {"missing": missing})
    _enforce_edge_scope(principal, payload)
    return store.record_heartbeat(payload)
