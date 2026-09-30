from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any
from dataclasses import dataclass
import hashlib
import hmac
import secrets
import httpx
import cv2
import numpy as np
from urllib.parse import quote

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field

from camera_service.licensing import sign_license_payload
from cloud_portal.storage import PortalStore
from cloud_portal.crm_client import crm_client
from camera_service.face_service import FaceService


def build_portal_store():
    database_url = os.getenv("SNAPKEY_DATABASE_URL", "").strip()
    if database_url:
        from cloud_portal.postgres_storage import PostgresPortalStore
        return PostgresPortalStore(database_url)
    if os.getenv("SNAPKEY_ENV", "development").strip().lower() == "production":
        raise RuntimeError("SNAPKEY_DATABASE_URL is required in production")
    return PortalStore(os.getenv("SNAPKEY_PORTAL_DB", "data/cloud_portal.db"))


store = build_portal_store()
app = FastAPI(title="SnapKey Vision AI Portal")
if os.getenv("SNAPKEY_ENABLE_CLOUD_INFERENCE", "0").strip() == "1":
    from cloud_portal.inference_api import router as inference_router
    app.include_router(inference_router)

DEFAULT_LICENSE_FEATURES = ["tracking", "attendance", "face_recognition", "unknown_detection", "shoplifting", "object_security", "cloud_sync", "alerts", "evidence_clips"]

class LicenseIssueRequest(BaseModel):
    tenant_id: str
    site_id: str
    edge_id: str
    machine_code: str
    plan: str = "professional"
    max_cameras: int = 7
    features: list[str] = Field(default_factory=lambda: DEFAULT_LICENSE_FEATURES.copy())
    days: int = 30
    grace_days: int = 7

class EdgeActivationRequest(BaseModel):
    company_code: str | None = None
    shop_code: str
    activation_code: str
    machine_code: str
    device_name: str | None = None

class EdgeActivationCodeRequest(BaseModel):
    expires_minutes: int = Field(default=30, ge=5, le=1440)


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

class PortalRegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str
    company_code: str
    shop_code: str
    activation_code: str

class PortalLoginRequest(BaseModel):
    email: str
    password: str

class CloudPersonCreate(BaseModel):
    employee_code: str = Field(min_length=1,max_length=64)
    full_name: str = Field(min_length=1,max_length=128)
    role: str = "WORKER"
    phone: str | None = None
    email: str | None = None

class CloudPersonPatch(BaseModel):
    full_name: str | None = None
    role: str | None = None
    phone: str | None = None
    email: str | None = None
    active: bool | None = None

_cloud_face_service: FaceService | None = None
def _cloud_face_enroller() -> FaceService:
    global _cloud_face_service
    if _cloud_face_service is None:
        _cloud_face_service=FaceService(None)
    return _cloud_face_service

class CrmPersonMappingRequest(BaseModel):
    local_person_id: str
    crm_user_id: str
    employee_code: str | None = None
    break_master_id: str | None = None

def _password_hash(password: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt=secrets.token_bytes(16)
    digest=hashlib.pbkdf2_hmac("sha256",password.encode("utf-8"),salt,210000)
    return salt.hex()+"$"+digest.hex()

def _password_valid(password: str, encoded: str) -> bool:
    try:
        salt_hex,digest_hex=encoded.split("$",1)
        actual=_password_hash(password,bytes.fromhex(salt_hex)).split("$",1)[1]
        return secrets.compare_digest(actual,digest_hex)
    except Exception:
        return False

def _native_session(user: dict[str, Any]) -> dict[str, Any]:
    session_id=secrets.token_urlsafe(18); token=secrets.token_urlsafe(32)
    now=datetime.now(timezone.utc); expires=now+timedelta(hours=12)
    store.create_portal_session({"session_id":session_id,"token_hash":_token_digest(token),
        "tenant_id":user["tenant_id"],"company_code":user.get("company_code"),"shop_id":user["shop_id"],
        "user_id":user["id"],"display_name":user["display_name"],"role":user["role"],
        "created_at":now.isoformat(),"expires_at":expires.isoformat()})
    return {"token":token,"sessionId":session_id,"expiresAt":expires.isoformat(),
        "tenantId":user["tenant_id"],"companyCode":user.get("company_code"),"shopCode":user["shop_id"],
        "displayName":user["display_name"],"role":user["role"]}

@app.post("/auth/register")
def register_portal_user(payload: PortalRegisterRequest, request: Request):
    if os.getenv("SNAPKEY_ENV", "production").lower() != "development" and not _crm_integration_key_valid(request.headers.get("X-CRM-Integration-Key")):
        raise HTTPException(403, "Account provisioning requires the trusted CRM backend")
    if not _edge_activation_code_valid(payload.activation_code):
        raise HTTPException(401, "Invalid Camera Eye activation code")
    email=payload.email.strip().lower(); password=payload.password
    if "@" not in email: raise HTTPException(400,"Enter a valid email address")
    if len(password)<8: raise HTTPException(400,"Password must be at least 8 characters")
    if store.portal_user_by_email(email): raise HTTPException(409,"An account already exists for this email")
    company=_slug(payload.company_code); shop=_slug(payload.shop_code)
    # Deliberately matches edge activation identity so an owner registering the same
    # company/shop immediately sees already-activated Camera Eye devices.
    user={"id":secrets.token_urlsafe(18),"email":email,"password_hash":_password_hash(password),
        "display_name":payload.display_name.strip() or email.split("@")[0],"tenant_id":f"tenant-{company}",
        "company_code":payload.company_code.strip(),"shop_id":shop,"role":"OWNER",
        "created_at":datetime.now(timezone.utc).isoformat()}
    store.create_portal_user(user)
    return _native_session(user)

@app.post("/auth/login")
def login_portal_user(payload: PortalLoginRequest):
    user=store.portal_user_by_email(payload.email.strip().lower())
    if not user or not _password_valid(payload.password,user["password_hash"]):
        raise HTTPException(401,"Invalid email or password")
    return _native_session(user)


def _crm_integration_key_valid(value: str | None) -> bool:
    expected=os.getenv("SNAPKEY_CRM_INTEGRATION_KEY","").strip()
    return bool(expected and value and secrets.compare_digest(value.strip(),expected))

def _edge_activation_code_valid(value: str | None) -> bool:
    expected=os.getenv("SNAPKEY_EDGE_ACTIVATION_CODE","").strip()
    return bool(expected and value and secrets.compare_digest(value.strip(),expected))

def _slug(value: str) -> str:
    cleaned="".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    return "-".join(part for part in cleaned.split("-") if part) or "default"

def _issue_license_payload(tenant_id: str, site_id: str, edge_id: str, machine_code: str,
                           plan: str = "professional", max_cameras: int = 7,
                           features: list[str] | None = None, days: int = 30,
                           grace_days: int = 7) -> dict[str, Any]:
    private_key = os.getenv("SNAPKEY_LICENSE_PRIVATE_KEY", "").strip()
    if not private_key:
        raise HTTPException(500, "SNAPKEY_LICENSE_PRIVATE_KEY is required to issue licenses")
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=max(1, days))
    grace = expires + timedelta(days=max(0, grace_days))
    payload = {
        "tenant_id": tenant_id,
        "site_id": site_id,
        "edge_id": edge_id,
        "machine_code": machine_code,
        "plan": plan,
        "max_cameras": max(1, max_cameras),
        "features": sorted({feature.strip().lower() for feature in (features or DEFAULT_LICENSE_FEATURES) if feature.strip()}),
        "issued_at": now.isoformat(),
        "expires_at": expires.isoformat(),
        "grace_until": grace.isoformat(),
    }
    return {"license": payload, "signature": sign_license_payload(payload, private_key)}


@app.post("/crm/session")
def create_crm_portal_session(payload: CrmPortalSessionRequest, request: Request):
    supplied=request.headers.get("X-CRM-Integration-Key","")
    if not _crm_integration_key_valid(supplied):
        raise HTTPException(401,"Invalid CRM integration key")
    shop=_slug(payload.shopCode)
    if not shop: raise HTTPException(400,"shopCode is required")
    # Camera Eye edge activation uses tenant-{company slug}. Prefer companyCode so
    # a CRM launch lands in the exact same tenant/shop as the activated Windows edge.
    company=(payload.companyCode or "").strip()
    tenant=(f"tenant-{_slug(company)}" if company else (payload.tenantId or "").strip())
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

@app.post("/edge/v1/activate")
def activate_edge(payload: EdgeActivationRequest):
    machine = payload.machine_code.strip()
    if not machine:
        raise HTTPException(400, "machine_code is required")
    one_time = store.consume_edge_activation_code(_token_digest(payload.activation_code.strip()), machine)
    if one_time:
        shop = str(one_time["shop_id"])
        company_code = one_time.get("company_code")
        company = _slug(company_code or shop)
        tenant_id = str(one_time["tenant_id"])
    elif os.getenv("SNAPKEY_ENV", "production").lower() == "development" and _edge_activation_code_valid(payload.activation_code):
        # Backward-compatible installer/dev activation. Production onboarding should use a one-time portal code.
        shop = _slug(payload.shop_code)
        company_code = payload.company_code
        company = _slug(company_code or shop)
        tenant_id = f"tenant-{company}"
    else:
        raise HTTPException(401, "Invalid or expired activation code")
    site_id = f"site-{shop}"
    edge_id = f"edge-{shop}-{_token_digest(machine)[:8]}"
    token = secrets.token_urlsafe(32)
    license_data = _issue_license_payload(
        tenant_id=tenant_id,
        site_id=site_id,
        edge_id=edge_id,
        machine_code=machine,
        days=int(os.getenv("SNAPKEY_DEFAULT_LICENSE_DAYS", "30")),
        grace_days=int(os.getenv("SNAPKEY_DEFAULT_LICENSE_GRACE_DAYS", "7")),
    )
    store.provision_edge_credential(_token_digest(token), tenant_id, company_code, shop, site_id, edge_id)
    return {
        "edge": {
            "tenant_id": tenant_id,
            "company_code": company_code,
            "shop_id": shop,
            "site_id": site_id,
            "edge_id": edge_id,
        },
        "cloud": {
            "base_url": os.getenv("SNAPKEY_PUBLIC_BASE_URL", "https://camera.snapkey.ai").rstrip("/") or "https://camera.snapkey.ai",
            "api_token": token,
        },
        "license": license_data["license"],
        "signature": license_data["signature"],
        "license_public_key": os.getenv("SNAPKEY_LICENSE_PUBLIC_KEY", "").strip(),
        "message": "Edge activated successfully",
    }


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


@app.post("/portal/v1/tenants/{tenant_id}/edge-activation-codes")
def create_edge_activation_code(tenant_id: str, payload: EdgeActivationCodeRequest, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    if principal.role.upper() not in {"OWNER", "ADMIN", "SUPERADMIN"}:
        raise HTTPException(403, "Only an owner or administrator can set up a new edge device")
    raw = secrets.token_urlsafe(9).replace("-", "").replace("_", "").upper()[:12]
    code = "CE-" + "-".join(raw[i:i+4] for i in range(0, len(raw), 4))
    now = datetime.now(timezone.utc)
    expires = now + timedelta(minutes=payload.expires_minutes)
    store.create_edge_activation_code({
        "code_hash": _token_digest(code),
        "tenant_id": principal.tenant_id,
        "company_code": principal.company_code,
        "shop_id": principal.shop_id,
        "created_by": principal.user_id,
        "created_at": now.isoformat(),
        "expires_at": expires.isoformat(),
    })
    return {"activationCode": code, "expiresAt": expires.isoformat(), "shopCode": principal.shop_id}


@app.get("/health")
def health():
    return {"status": "ok", "service": "snapkey-portal"}


PORTAL_STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=PORTAL_STATIC_DIR), name="static")


@app.get("/", include_in_schema=False)
@app.get("/portal", include_in_schema=False)
def portal_home():
    return FileResponse(PORTAL_STATIC_DIR / "index.html")

@app.get("/login", include_in_schema=False)
def portal_login():
    return FileResponse(PORTAL_STATIC_DIR / "login.html")


@app.get("/portal/{page_name}", include_in_schema=False)
def portal_page(page_name: str):
    allowed = {"index.html", "cameras.html", "personnel.html", "attendance.html", "live.html", "alerts.html"}
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



def _portal_scope(tenant_id: str, principal: PortalPrincipal) -> None:
    if principal.tenant_id != tenant_id:
        raise HTTPException(403,"Portal session is not authorized for this tenant")


def _portal_admin(principal: PortalPrincipal) -> None:
    if principal.role.upper() not in {'OWNER','ADMIN','SUPERADMIN'}:
        raise HTTPException(403,'Administrator access is required')


def _portal_camera_view(camera: dict[str, Any]) -> dict[str, Any]:
    """Return browser-safe camera metadata without exposing connection credentials."""
    view = dict(camera)
    source = str(view.pop("source", "") or "")
    view["source_configured"] = bool(source)
    view["source_is_edge_local"] = source.startswith("edge-local:")
    return view


@app.get("/portal/v1/tenants/{tenant_id}/cameras")
def portal_cameras(tenant_id: str, shop_id: str | None = None, edge_id: str | None = None, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    if shop_id and shop_id != principal.shop_id: raise HTTPException(403,"Portal session is not authorized for this shop")
    return {"items": [_portal_camera_view(camera) for camera in store.list_cameras(tenant_id, shop_id=principal.shop_id, edge_id=edge_id)]}


@app.put("/portal/v1/tenants/{tenant_id}/cameras/{camera_id}")
def save_portal_camera(tenant_id: str, camera_id: str, request: PortalCameraConfig, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id,principal)
    if request.shop_id != principal.shop_id: raise HTTPException(403,"Portal session is not authorized for this shop")
    if request.tenant_id != tenant_id or request.camera_id != camera_id:
        raise HTTPException(400, "Camera scope does not match request path")
    if request.source_type not in {"rtsp", "file", "webcam", "dshow"}:
        raise HTTPException(400, "Unsupported camera source type")
    payload = request.model_dump()
    settings = dict(request.settings)
    settings['tracking_mode'] = 'track' if settings.get('tracking_mode') == 'track' else 'detect'
    try:
        quality = settings.get('tracking_quality', 65)
        quality = {'performance':55, 'balanced':65, 'high':85}.get(str(quality),quality)
        settings['tracking_quality'] = max(35,min(95,int(quality)))
        settings['tracking_fps'] = max(1,min(12,float(settings.get('tracking_fps',3))))
        settings['tracking_imgsz'] = max(256,min(640,int(settings.get('tracking_imgsz',settings.get('max_frame_width',384)))))
    except (ValueError,TypeError,OverflowError):
        raise HTTPException(422,'Invalid camera performance settings')
    payload['settings'] = settings
    if request.source == "__KEEP_EXISTING__":
        existing = next((
            camera for camera in store.list_cameras(tenant_id, shop_id=principal.shop_id, edge_id=request.edge_id)
            if str(camera.get("camera_id")) == camera_id
        ), None)
        if not existing or not str(existing.get("source") or "").strip():
            raise HTTPException(400, "Existing camera source could not be preserved")
        payload["source"] = existing["source"]
    elif not request.source.strip():
        raise HTTPException(400, "Camera source is required")
    return {"camera": _portal_camera_view(store.upsert_camera(payload))}


@app.delete("/portal/v1/tenants/{tenant_id}/cameras/{camera_id}")
def delete_portal_camera(tenant_id: str, camera_id: str, shop_id: str, edge_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id,principal)
    if shop_id != principal.shop_id: raise HTTPException(403,"Portal session is not authorized for this shop")
    existing = store.get_camera(tenant_id, shop_id, edge_id, camera_id)
    if existing:
        store.create_edge_command({"tenant_id": tenant_id, "shop_id": shop_id, "edge_id": edge_id,
            "command_type": "CAMERA_DELETE", "request": {"camera_id": camera_id}})
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
def create_portal_edge_command(tenant_id: str, request: EdgeCommandRequest, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id,principal)
    if request.shop_id != principal.shop_id: raise HTTPException(403,"Portal session is not authorized for this shop")
    if request.tenant_id != tenant_id:
        raise HTTPException(400, "Command tenant does not match request path")
    if request.command_type not in {"ONVIF_PROBE", "CAMERA_TEST"}:
        raise HTTPException(400, "Unsupported edge command")
    return store.create_edge_command(request.model_dump())


@app.get("/portal/v1/tenants/{tenant_id}/edge-commands/{command_id}")
def portal_edge_command(tenant_id: str, command_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    command=store.get_edge_command(command_id, tenant_id, principal.shop_id)
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


@app.get("/edge/v1/config/personnel")
def edge_personnel_config(principal: EdgePrincipal = Depends(require_edge_token)):
    if principal.legacy_global: raise HTTPException(403,"Scoped edge credential is required for personnel configuration")
    items=[]
    for person in store.list_cloud_people(principal.tenant_id,principal.shop_id):
        faces=store.list_cloud_faces(principal.tenant_id,principal.shop_id,str(person["id"]),include_embedding=True)
        items.append({"person_id":str(person["id"]),"employee_code":person["employee_code"],"full_name":person["full_name"],
            "role":person["role"],"phone":person.get("phone"),"email":person.get("email"),"active":bool(person["active"]),
            "faces":[{"face_id":str(f["id"]),"embedding":f["embedding"],"quality":float(f["quality"])} for f in faces]})
    return {"tenant_id":principal.tenant_id,"shop_id":principal.shop_id,"items":items}

@app.post("/edge/v1/events/{event_id}/evidence")
async def upload_edge_event_evidence(
    event_id: str,
    file: UploadFile = File(...),
    principal: EdgePrincipal = Depends(require_edge_token),
):
    if principal.legacy_global:
        raise HTTPException(403, "Scoped edge credential is required for evidence upload")
    safe_event_id = "".join(ch for ch in event_id if ch.isalnum() or ch in {"-", "_", ":"})[:160]
    if not safe_event_id:
        raise HTTPException(400, "Invalid event_id")
    content_type = (file.content_type or "").lower()
    allowed = {"image/jpeg", "image/jpg", "image/png", "image/webp", "video/mp4"}
    if content_type not in allowed:
        raise HTTPException(415, "Only JPEG, PNG, WebP, and MP4 evidence are supported")
    max_bytes = 50 * 1024 * 1024 if content_type == "video/mp4" else 5 * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, "Evidence file exceeds the allowed size")
    suffix = {"image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4"}.get(content_type, ".jpg")
    root = Path(os.getenv("SNAPKEY_EVIDENCE_ROOT", "/app/data/evidence"))
    root = root.resolve()
    target_dir = (root / str(principal.tenant_id) / str(principal.shop_id) / str(principal.edge_id)).resolve()
    if not target_dir.is_relative_to(root):
        raise HTTPException(403, 'Invalid evidence scope')
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{safe_event_id}{suffix}"
    target.write_bytes(data)
    return {
        "event_id": event_id,
        "evidence_id": f"{principal.tenant_id}/{principal.shop_id}/{principal.edge_id}/{safe_event_id}{suffix}",
        "content_type": content_type,
        "size_bytes": len(data),
    }


def _deliver_crm_attendance_event(envelope: dict[str, Any]) -> None:
    if not crm_client.configured:
        return
    event_type=str(envelope.get("event_type") or "")
    if event_type not in {"ATTENDANCE_ENTRY","ATTENDANCE_EXIT","BREAK_START","BREAK_END"}:
        return
    payload=envelope.get("payload") or {}
    local_person_id=str(payload.get("person_id") or envelope.get("person_id") or "").strip()
    if not local_person_id:
        return
    tenant_id=str(envelope.get("tenant_id") or "")
    shop_id=str(envelope.get("shop_id") or "")
    mapping=store.crm_person_mapping(tenant_id,shop_id,local_person_id)
    if not mapping:
        return
    event_time=datetime.fromisoformat(str(envelope["event_time"]).replace("Z","+00:00"))
    if event_time.tzinfo is None:
        event_time=event_time.replace(tzinfo=timezone.utc)
    event_time=event_time.astimezone(timezone.utc)
    # SnapKey UserRoster/LoginLogout accepts one payload for both mutations.
    # Preserve the camera event timestamp as an ISO-8601 UTC value: ENTRY fills
    # actualStartTime; EXIT fills actualOffTime. The unused fields are empty
    # strings, matching the CRM contract supplied by the customer.
    crm_timestamp=event_time.isoformat(timespec="milliseconds").replace("+00:00","Z")
    crm_user_id=mapping["crm_user_id"]
    if event_type in {"ATTENDANCE_ENTRY", "ATTENDANCE_EXIT"} and os.getenv("SNAPKEY_CRM_ATTENDANCE_ENABLED", "0").strip() != "1":
        return
    location="Camera Eye - "+str(envelope.get("site_id") or shop_id)
    if event_type=="ATTENDANCE_ENTRY":
        crm_client.login_logout({"userId":crm_user_id,"date":crm_timestamp,
            "actualStartTime":crm_timestamp,"actualOffTime":"",
            "loginLocation":location,"logoutLocation":""})
    elif event_type=="ATTENDANCE_EXIT":
        crm_client.login_logout({"userId":crm_user_id,"date":crm_timestamp,
            "actualStartTime":"","actualOffTime":crm_timestamp,
            "loginLocation":"","logoutLocation":location})
    elif event_type=="BREAK_START":
        # Current edge track-loss events are not sufficiently strong evidence of a real break.
        # Only explicitly confirmed break events may mutate CRM break state.
        metadata=payload.get("metadata") or {}
        if metadata.get("crm_confirmed_break") is True and mapping.get("break_master_id"):
            crm_client.start_break(crm_user_id,mapping["break_master_id"])
    elif event_type=="BREAK_END":
        metadata=payload.get("metadata") or {}
        if metadata.get("crm_confirmed_break") is True:
            crm_client.end_break(crm_user_id)


@app.post("/edge/v1/events")
def ingest_edge_event(envelope: dict[str, Any], background_tasks: BackgroundTasks,
                      principal: EdgePrincipal = Depends(require_edge_token)):
    required = ["schema_version", "tenant_id", "site_id", "edge_id", "event_id", "event_type", "event_time", "payload"]
    missing = [key for key in required if not envelope.get(key)]
    if missing:
        raise HTTPException(400, {"missing": missing})
    if envelope["schema_version"] != "edge.event.v1":
        raise HTTPException(400, "Unsupported event schema")
    _enforce_edge_scope(principal, envelope)
    result=store.ingest_event(envelope)
    # Edge delivery is at-least-once. Only the first successful insert may create a
    # downstream CRM mutation; retries of the same event_id are acknowledged without
    # scheduling another login/logout/break call.
    if result.get("inserted", True):
        background_tasks.add_task(_deliver_crm_attendance_event,envelope)
    return result


@app.get("/portal/v1/tenants/{tenant_id}/personnel")
def portal_personnel(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    mappings={str(x["local_person_id"]):x for x in store.list_crm_person_mappings(tenant_id,principal.shop_id)}
    edge_people={}
    for edge in store.list_edges(tenant_id,shop_id=principal.shop_id):
        for person in (edge.get("status") or {}).get("personnel") or []:
            edge_people.setdefault(str(person.get("person_id") or ""),[]).append(str(edge.get("edge_id") or ""))
    items=[]
    for person in store.list_cloud_people(tenant_id,principal.shop_id):
        pid=str(person["id"]); item=dict(person)
        item["person_id"]=pid; item["edge_ids"]=sorted(set(edge_people.get(pid,[])))
        item["edge_synced"]=bool(item["edge_ids"]); item["crm_mapping"]=mappings.get(pid)
        item["face_enrolled"]=int(item.get("face_count") or 0)>0
        items.append(item)
    return {"items":items}

@app.post("/portal/v1/tenants/{tenant_id}/personnel")
def create_portal_person(tenant_id: str, request: CloudPersonCreate, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id,principal)
    role=request.role.strip().upper()
    if role not in {"OWNER","MANAGER","WORKER"}: raise HTTPException(400,"Role must be OWNER, MANAGER, or WORKER")
    try:
        return store.create_cloud_person({"id":secrets.token_urlsafe(18),"tenant_id":tenant_id,"shop_id":principal.shop_id,
            "employee_code":request.employee_code.strip(),"full_name":request.full_name.strip(),"role":role,
            "phone":request.phone,"email":request.email})
    except Exception as exc:
        if "unique" in str(exc).lower() or "duplicate" in str(exc).lower(): raise HTTPException(409,"Employee code already exists in this shop")
        raise

@app.patch("/portal/v1/tenants/{tenant_id}/personnel/{person_id}")
def patch_portal_person(tenant_id: str, person_id: str, request: CloudPersonPatch, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id,principal)
    item=store.update_cloud_person(tenant_id,principal.shop_id,person_id,request.model_dump(exclude_unset=True))
    if not item: raise HTTPException(404,"Person not found")
    return item

@app.delete("/portal/v1/tenants/{tenant_id}/personnel/{person_id}")
def deactivate_portal_person(tenant_id: str, person_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id,principal)
    item=store.update_cloud_person(tenant_id,principal.shop_id,person_id,{"active":False})
    if not item: raise HTTPException(404,"Person not found")
    return {"ok":True}

@app.post("/portal/v1/tenants/{tenant_id}/personnel/{person_id}/faces")
async def enroll_portal_face(tenant_id: str, person_id: str, file: UploadFile=File(...), principal: PortalPrincipal=Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id,principal)
    if not store.get_cloud_person(tenant_id,principal.shop_id,person_id): raise HTTPException(404,"Person not found")
    if (file.content_type or "").lower() not in {"image/jpeg","image/jpg","image/png","image/webp"}: raise HTTPException(415,"Unsupported image type")
    raw=await file.read(8*1024*1024+1)
    if len(raw)>8*1024*1024: raise HTTPException(413,"Image exceeds 8 MB")
    image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR)
    if image is None: raise HTTPException(400,"Invalid image")
    try: embedding,quality=_cloud_face_enroller().enroll(image)
    except ValueError as exc: raise HTTPException(400,str(exc))
    face_id=secrets.token_urlsafe(18)
    root=Path(os.getenv("SNAPKEY_EVIDENCE_ROOT","/app/data/evidence"))/"personnel"/tenant_id/principal.shop_id/person_id
    root.mkdir(parents=True,exist_ok=True); path=root/(face_id+".jpg")
    cv2.imwrite(str(path),image)
    face=store.add_cloud_face({"id":face_id,"person_id":person_id,"tenant_id":tenant_id,"shop_id":principal.shop_id,
        "embedding":embedding,"quality":quality,"image_path":str(path)})
    return {**face,"image_url":f"/portal/v1/tenants/{tenant_id}/personnel/{person_id}/faces/{face_id}/image"}

@app.get("/portal/v1/tenants/{tenant_id}/personnel/{person_id}/faces/{face_id}/image")
def portal_face_image(tenant_id: str,person_id: str,face_id: str,principal: PortalPrincipal=Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    faces=store.list_cloud_faces(tenant_id,principal.shop_id,person_id)
    face=next((x for x in faces if str(x["id"])==face_id),None)
    if not face or not face.get("image_path") or not Path(face["image_path"]).is_file(): raise HTTPException(404,"Face image not found")
    return FileResponse(face["image_path"],media_type="image/jpeg")

@app.get("/portal/v1/tenants/{tenant_id}/attendance")
def portal_attendance(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    people={str(p["id"]):p for p in store.list_cloud_people(tenant_id,principal.shop_id)}
    raw=store.list_events(tenant_id,limit=500,shop_id=principal.shop_id)
    sessions={}; person_events=[]
    for event in raw:
        event_type=str(event.get("event_type") or "")
        payload=event.get("payload") or {}
        inner=payload.get("payload") if isinstance(payload.get("payload"),dict) else payload
        person_id=str(inner.get("person_id") or "")
        metadata=inner.get("metadata") or {}
        person=people.get(person_id,{})
        if event_type in {"ATTENDANCE_ENTRY","ATTENDANCE_EXIT"}:
            sid=str(metadata.get("attendance_session_id") or event.get("id") or "")
            record=sessions.setdefault(sid,{"session_id":sid,"person_id":person_id,"full_name":person.get("full_name") or person_id or "Unknown",
                "employee_code":person.get("employee_code") or "—","role":person.get("role") or "—","entry_time":None,"exit_time":None})
            if event_type=="ATTENDANCE_ENTRY": record["entry_time"]=str(event.get("event_time") or "")
            else: record["exit_time"]=str(event.get("event_time") or "")
        if event_type in {"ATTENDANCE_ENTRY","ATTENDANCE_EXIT","BREAK_START","BREAK_END","PERSON_RECOGNIZED"}:
            person_events.append({"event_id":event.get("id"),"event_type":event_type,"event_time":str(event.get("event_time") or ""),
                "person_id":person_id,"full_name":person.get("full_name") or person_id or "Unknown","employee_code":person.get("employee_code") or "—",
                "camera_id":event.get("camera_id"),"confidence":metadata.get("confidence"),"has_evidence":bool(metadata.get("cloud_evidence"))})
    records=sorted(sessions.values(),key=lambda x:x.get("entry_time") or x.get("exit_time") or "",reverse=True)
    presence=[{**r,"status":"PRESENT"} for r in records if r.get("entry_time") and not r.get("exit_time")]
    return {"records":records,"presence":presence,"events":person_events[:100]}

@app.get("/portal/v1/tenants/{tenant_id}/crm/status")
def crm_status(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    return {"configured":crm_client.configured,"base_url":crm_client.base_url,
            "mapping_count":len(store.list_crm_person_mappings(tenant_id,principal.shop_id))}

@app.get("/portal/v1/tenants/{tenant_id}/crm/breaks")
def crm_breaks(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    if not crm_client.configured: raise HTTPException(503,"SnapKey CRM API token is not configured")
    try: return {"items":crm_client.my_breaks()}
    except httpx.HTTPError as exc: raise HTTPException(502,f"SnapKey CRM break lookup failed: {exc}") from exc

@app.get("/portal/v1/tenants/{tenant_id}/crm/face-embeddings/{employee_code}")
def crm_face_embeddings(tenant_id: str, employee_code: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    if not crm_client.configured: raise HTTPException(503,"SnapKey CRM API token is not configured")
    try: return {"employee_code":employee_code,"data":crm_client.face_embeddings(employee_code)}
    except httpx.HTTPError as exc: raise HTTPException(502,f"SnapKey CRM face-embedding lookup failed: {exc}") from exc


@app.get("/portal/v1/tenants/{tenant_id}/crm/person-mappings")
def crm_person_mappings(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    return {"items":store.list_crm_person_mappings(tenant_id,principal.shop_id)}

@app.put("/portal/v1/tenants/{tenant_id}/crm/person-mappings/{local_person_id}")
def crm_person_mapping(tenant_id: str, local_person_id: str, request: CrmPersonMappingRequest,
                       principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id, principal)
    if request.local_person_id != local_person_id: raise HTTPException(400,"local_person_id does not match path")
    if not request.crm_user_id.strip(): raise HTTPException(400,"crm_user_id is required")
    return {"mapping":store.upsert_crm_person_mapping({
        "tenant_id":tenant_id,"shop_id":principal.shop_id,"local_person_id":local_person_id,
        "crm_user_id":request.crm_user_id.strip(),"employee_code":(request.employee_code or "").strip() or None,
        "break_master_id":(request.break_master_id or "").strip() or None,
    })}

@app.post("/portal/v1/tenants/{tenant_id}/crm/test-break-start/{local_person_id}")
def crm_test_break_start(tenant_id: str, local_person_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id, principal)
    mapping=store.crm_person_mapping(tenant_id,principal.shop_id,local_person_id)
    if not mapping: raise HTTPException(404,"CRM person mapping not found")
    if not mapping.get("break_master_id"): raise HTTPException(400,"No CRM breakMasterId is mapped")
    if not crm_client.configured: raise HTTPException(503,"SnapKey CRM API token is not configured")
    try: return {"result":crm_client.start_break(mapping["crm_user_id"],mapping["break_master_id"])}
    except httpx.HTTPError as exc: raise HTTPException(502,f"SnapKey CRM start-break failed: {exc}") from exc

@app.post("/portal/v1/tenants/{tenant_id}/crm/test-break-end/{local_person_id}")
def crm_test_break_end(tenant_id: str, local_person_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id, principal)
    mapping=store.crm_person_mapping(tenant_id,principal.shop_id,local_person_id)
    if not mapping: raise HTTPException(404,"CRM person mapping not found")
    if not crm_client.configured: raise HTTPException(503,"SnapKey CRM API token is not configured")
    try: return {"result":crm_client.end_break(mapping["crm_user_id"])}
    except httpx.HTTPError as exc: raise HTTPException(502,f"SnapKey CRM end-break failed: {exc}") from exc


@app.get("/portal/v1/tenants/{tenant_id}/summary")
def tenant_summary(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    return store.tenant_summary(tenant_id, shop_id=principal.shop_id)


@app.get("/portal/v1/tenants/{tenant_id}/events")
def tenant_events(tenant_id: str, site_id: str | None = None, event_type: str | None = None, limit: int = 100,
                  principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    items = store.list_events(tenant_id, site_id=site_id, event_type=event_type, limit=limit, shop_id=principal.shop_id)
    # Backward-compatible storage implementations may not accept shop_id yet, so enforce
    # the authenticated shop boundary before returning any event to the browser.
    return {"items": [item for item in items if str(item.get("shop_id") or item.get("site_id") or "") == str(principal.shop_id)]}


@app.get("/portal/v1/tenants/{tenant_id}/edges")
def portal_edges(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    return {"items": store.list_edges(tenant_id, shop_id=principal.shop_id)}


@app.get("/portal/v1/tenants/{tenant_id}/events/{event_id}/evidence")
def portal_event_evidence(tenant_id: str, event_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    event = store.get_event(tenant_id, principal.shop_id, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    envelope = event["payload"]
    evidence = (envelope.get("payload", envelope).get("metadata") or {}).get("cloud_evidence") or {}
    evidence_id = evidence.get("evidence_id")
    if not evidence_id:
        raise HTTPException(404, "Snapshot not available")
    root = Path(os.getenv("SNAPKEY_EVIDENCE_ROOT", "/app/data/evidence")).resolve()
    allowed = (root / tenant_id / principal.shop_id / event["edge_id"]).resolve()
    path = (root / evidence_id).resolve()
    if not allowed.is_relative_to(root) or not path.is_relative_to(allowed) or not path.is_file():
        raise HTTPException(404, "Snapshot not available")
    return FileResponse(path, headers={"Cache-Control": "private, no-store"})


@app.get("/portal/v1/tenants/{tenant_id}/events/{event_id}/clip")
def portal_event_clip(tenant_id: str, event_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    event = store.get_event(tenant_id, principal.shop_id, event_id)
    if not event:
        raise HTTPException(404, "Event not found")
    envelope = event["payload"]
    evidence = (envelope.get("payload", envelope).get("metadata") or {}).get("cloud_clip") or {}
    evidence_id = evidence.get("evidence_id")
    if not evidence_id:
        raise HTTPException(404, "Video clip not available")
    root = Path(os.getenv("SNAPKEY_EVIDENCE_ROOT", "/app/data/evidence")).resolve()
    allowed = (root / tenant_id / principal.shop_id / event["edge_id"]).resolve()
    path = (root / evidence_id).resolve()
    if not allowed.is_relative_to(root) or not path.is_relative_to(allowed) or not path.is_file():
        raise HTTPException(404, "Video clip not available")
    return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, no-store"})


@app.post("/portal/v1/licenses/issue")
def issue_license(request: LicenseIssueRequest, http_request: Request):
    supplied=http_request.headers.get("X-CRM-Integration-Key","")
    if not _crm_integration_key_valid(supplied):
        raise HTTPException(401, "Administrative integration key is required")
    return _issue_license_payload(
        tenant_id=request.tenant_id,
        site_id=request.site_id,
        edge_id=request.edge_id,
        machine_code=request.machine_code,
        plan=request.plan,
        max_cameras=request.max_cameras,
        features=request.features,
        days=request.days,
        grace_days=request.grace_days,
    )


@app.post("/edge/v1/heartbeat")
def edge_heartbeat(payload: dict[str, Any], principal: EdgePrincipal = Depends(require_edge_token)):
    required = ["tenant_id", "site_id", "edge_id", "status"]
    missing = [key for key in required if payload.get(key) is None]
    if missing:
        raise HTTPException(400, {"missing": missing})
    _enforce_edge_scope(principal, payload)
    return store.record_heartbeat(payload)
