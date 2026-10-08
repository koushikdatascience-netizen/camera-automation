from __future__ import annotations

import base64
import json
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Any
from dataclasses import dataclass
import hashlib
import hmac
import secrets
import threading
import time as monotonic_time
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
from cloud_portal.attendance_policy import AttendancePolicy
from cloud_portal.notifications import NotificationService
from camera_service.face_service import FaceService

logger = logging.getLogger("camera_eye.portal")
logger.setLevel(logging.INFO)


def build_portal_store():
    database_url = os.getenv("SNAPKEY_DATABASE_URL", "").strip()
    if database_url:
        from cloud_portal.postgres_storage import PostgresPortalStore
        return PostgresPortalStore(database_url)
    if os.getenv("SNAPKEY_ENV", "development").strip().lower() == "production":
        raise RuntimeError("SNAPKEY_DATABASE_URL is required in production")
    return PortalStore(os.getenv("SNAPKEY_PORTAL_DB", "data/cloud_portal.db"))


store = build_portal_store()
notification_service = NotificationService()
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

class ClientLoginOptionsRequest(BaseModel):
    client_id: str = Field(min_length=1,max_length=128)

class ClientSiteLoginRequest(BaseModel):
    client_id: str = Field(min_length=1,max_length=128)
    user_id: str = Field(min_length=1,max_length=256)
    shop_code: str = Field(min_length=1,max_length=128)
    password: str = Field(min_length=1,max_length=256)

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
_crm_personnel_refresh_guard = threading.Lock()
_crm_personnel_refresh_locks: dict[tuple[str, str], threading.Lock] = {}
_crm_personnel_last_refresh: dict[tuple[str, str], float] = {}
_crm_face_image_fingerprints: set[tuple[str, str, str, str]] = set()

def _cloud_face_enroller() -> FaceService:
    global _cloud_face_service
    if _cloud_face_service is None:
        _cloud_face_service=FaceService(None)
    return _cloud_face_service

def _crm_personnel_refresh_lock(tenant_id: str, shop_id: str) -> threading.Lock:
    key=(tenant_id,shop_id)
    with _crm_personnel_refresh_guard:
        return _crm_personnel_refresh_locks.setdefault(key,threading.Lock())

def _crm_personnel_refresh_due(tenant_id: str, shop_id: str) -> bool:
    ttl=max(15,int(os.getenv("SNAPKEY_CRM_PERSONNEL_REFRESH_SECONDS","60")))
    last=_crm_personnel_last_refresh.get((tenant_id,shop_id),0.0)
    return monotonic_time.monotonic()-last >= ttl

class CrmPersonMappingRequest(BaseModel):
    local_person_id: str
    crm_user_id: str
    employee_code: str | None = None
    break_master_id: str | None = None

class AttendanceStationActionRequest(BaseModel):
    camera_id: str
    edge_id: str
    recognition_event_id: str
    action: str


class AttendancePolicyRequest(BaseModel):
    grace_period_minutes: int = Field(default=15, ge=1, le=240)
    allowed_break_minutes: int = Field(default=60, ge=0, le=480)
    total_working_minutes: int = Field(default=480, ge=1, le=1440)
    max_logoff_time: str = Field(default="21:30", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    absence_auto_logout_enabled: bool = True
    timezone: str = "Asia/Kolkata"
    email_recipients: list[str] = Field(default_factory=list, max_length=20)
    whatsapp_recipients: list[str] = Field(default_factory=list, max_length=20)



class PersonAttendancePolicyRequest(BaseModel):
    attendanceMode: str = Field(default="AUTO", pattern="^(AUTO|MANUAL)$")
    presenceUpdateIntervalMinutes: int = Field(default=2, ge=1, le=60)
    outOfCameraGraceMinutes: int = Field(default=5, ge=1, le=1438)
    maxOutOfCameraOccurrencesPerDay: int = Field(default=5, ge=1, le=100)
    adminNotificationAfterMinutes: int = Field(default=15, ge=2, le=1439)
    markAbsentAfterMinutes: int = Field(default=60, ge=3, le=1440)
    requiredWorkingMinutes: int = Field(default=540, ge=1, le=1440)
    dayEndAutoLogoutEnabled: bool = True
    absenceMonitoringEnabled: bool = True
    timezone: str = "Asia/Kolkata"
    emailNotificationsEnabled: bool = True
    whatsappNotificationsEnabled: bool = True

class AttendanceLiveStartRequest(BaseModel):
    camera_id: str
    edge_id: str
    ttl_seconds: int = Field(default=600, ge=30, le=1800)


class AttendanceLiveStopRequest(BaseModel):
    camera_id: str
    edge_id: str
    session_id: str


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


@app.post("/auth/client/options")
def client_login_options(payload: ClientLoginOptionsRequest):
    if not hasattr(store,"client_login_options"):
        raise HTTPException(503,"Client/site login requires PostgreSQL portal storage")
    result=store.client_login_options(payload.client_id.strip())
    # Keep the response deliberately minimal: no email addresses, password data or tenant internals.
    return {"users":result.get("users") or [],"sites":result.get("sites") or []}

@app.post("/auth/client/login")
def client_site_login(payload: ClientSiteLoginRequest):
    if not hasattr(store,"portal_user_for_client_site"):
        raise HTTPException(503,"Client/site login requires PostgreSQL portal storage")
    user=store.portal_user_for_client_site(payload.client_id.strip(),payload.user_id.strip(),_slug(payload.shop_code))
    if not user or not _password_valid(payload.password,user["password_hash"]):
        raise HTTPException(401,"Invalid client, user, site, or password")
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


def _require_crm_integration(request: Request, tenant_id: str, shop_id: str) -> None:
    if not _crm_integration_key_valid(request.headers.get("X-CRM-Integration-Key")):
        raise HTTPException(401, "Invalid CRM integration key")
    configured=os.getenv("SNAPKEY_CRM_INTEGRATION_ALLOWED_SCOPES","").strip()
    if not configured:
        # Preserve local developer fixtures, but never permit an unscoped key in
        # production. Scope config is an allow-list attached to the server key.
        if os.getenv("SNAPKEY_ENV","").lower()=="production":
            raise HTTPException(503,"CRM integration tenant/shop scopes are not configured")
        return
    try:
        scopes=json.loads(configured)
    except ValueError as exc:
        raise HTTPException(503,"CRM integration scope configuration is invalid") from exc
    if not isinstance(scopes,list):
        raise HTTPException(503,"CRM integration scope configuration must be a JSON list")
    allowed=any(
        isinstance(item,dict)
        and str(item.get("tenant_id") or "")==tenant_id
        and shop_id in [str(value) for value in (item.get("shop_ids") or [])]
        for item in scopes
    )
    if not allowed:
        logger.warning("CRM_INTEGRATION_SCOPE_DENIED tenant_id=%s shop_id=%s",tenant_id,shop_id)
        raise HTTPException(403,"CRM integration key is not authorized for this tenant/shop")
    logger.info("CRM_INTEGRATION_ACCESS tenant_id=%s shop_id=%s method=%s path=%s",
                tenant_id,shop_id,request.method,request.url.path)


@app.put("/integration/v1/tenants/{tenant_id}/shops/{shop_id}/attendance-policy")
def integration_put_attendance_policy(tenant_id: str, shop_id: str, payload: AttendancePolicyRequest, request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    try:
        ZoneInfo(payload.timezone)
    except Exception as exc:
        raise HTTPException(400, "Invalid IANA timezone") from exc
    policy=payload.model_dump()
    saved=store.upsert_attendance_policy(tenant_id,shop_id,policy)
    logger.info(
        "ATTENDANCE_POLICY_UPDATED tenant_id=%s shop_id=%s grace=%s break=%s working=%s max_logoff=%s auto_logout=%s",
        tenant_id,shop_id,payload.grace_period_minutes,payload.allowed_break_minutes,
        payload.total_working_minutes,payload.max_logoff_time,payload.absence_auto_logout_enabled,
    )
    return {"policy":saved}


@app.get("/integration/v1/tenants/{tenant_id}/shops/{shop_id}/attendance-policy")
def integration_get_attendance_policy(tenant_id: str, shop_id: str, request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    return {"policy":store.attendance_policy(tenant_id,shop_id)}



@app.put("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/policy")
def integration_put_person_attendance_policy(tenant_id: str, shop_id: str, crm_user_id: str,
                                             payload: PersonAttendancePolicyRequest, request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    from cloud_portal.person_attendance_rules import PersonAttendancePolicy
    try:
        PersonAttendancePolicy(
            attendance_mode=payload.attendanceMode,
            presence_update_interval_minutes=payload.presenceUpdateIntervalMinutes,
            out_of_camera_grace_minutes=payload.outOfCameraGraceMinutes,
            max_out_of_camera_occurrences_per_day=payload.maxOutOfCameraOccurrencesPerDay,
            admin_notification_after_minutes=payload.adminNotificationAfterMinutes,
            mark_absent_after_minutes=payload.markAbsentAfterMinutes,
            required_working_minutes=payload.requiredWorkingMinutes,
            timezone=payload.timezone,
        )
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if not all((tenant_id.strip(), shop_id.strip(), crm_user_id.strip())):
        raise HTTPException(422, "Tenant, shop and CRM user ID are required")
    if not hasattr(store, "upsert_person_attendance_policy"):
        raise HTTPException(503, "Person-wise policies require PostgreSQL")
    saved=store.upsert_person_attendance_policy(tenant_id, shop_id, crm_user_id, payload.model_dump())
    logger.info("V2_ATTENDANCE_POLICY_UPDATED tenant_id=%s shop_id=%s crm_user_id=%s version=%s",
                tenant_id,shop_id,crm_user_id,saved.get("version"))
    return {"policy": saved}


@app.get("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/policy")
def integration_get_person_attendance_policy(tenant_id: str, shop_id: str, crm_user_id: str, request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    if not hasattr(store, "person_attendance_policy"):
        raise HTTPException(503, "Person-wise policies require PostgreSQL")
    saved=store.person_attendance_policy(tenant_id, shop_id, crm_user_id)
    if saved is None:
        raise HTTPException(404, "No person-specific policy configured")
    return {"policy": saved}



def _attendance_day_window(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    zone=ZoneInfo(timezone_name)
    start=datetime.combine(day,time.min,tzinfo=zone)
    return start.astimezone(timezone.utc),(start+timedelta(days=1)).astimezone(timezone.utc)


@app.get("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/activities")
def integration_person_attendance_activities(tenant_id: str, shop_id: str, crm_user_id: str,
                                             request: Request, day: date,
                                             page: int = 1, page_size: int = 50):
    _require_crm_integration(request,tenant_id,shop_id)
    if page < 1 or not 1 <= page_size <= 200:
        raise HTTPException(422, "Invalid pagination")
    policy=store.person_attendance_policy(tenant_id,shop_id,crm_user_id)
    zone=(policy or {}).get("timezone") or "Asia/Kolkata"
    start,end=_attendance_day_window(day,zone)
    items=store.list_person_attendance_activities(tenant_id,shop_id,crm_user_id,start,end,
                                                   page_size,(page-1)*page_size)
    return {"items":items,"page":page,"pageSize":page_size,"date":day.isoformat(),"timezone":zone}


@app.get("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/presence")
def integration_person_attendance_presence(tenant_id: str, shop_id: str, crm_user_id: str,
                                           request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    presence=store.get_person_attendance_presence(tenant_id,shop_id,crm_user_id)
    if presence is None:
        raise HTTPException(404,"No presence record")
    return {"presence":presence,"cameraCoverageHealth":"NOT_EVALUATED",
            "note":"Last seen is not proof of current presence; use camera health before absence decisions"}


@app.get("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/daily-summary")
def integration_person_attendance_summary(tenant_id: str, shop_id: str, crm_user_id: str,
                                          request: Request, day: date):
    _require_crm_integration(request,tenant_id,shop_id)
    from cloud_portal.working_time import calculate_working_time
    policy=store.person_attendance_policy(tenant_id,shop_id,crm_user_id)
    zone=(policy or {}).get("timezone") or "Asia/Kolkata"
    start,end=_attendance_day_window(day,zone)
    # Fetch the prior day as well so a shift begun before local midnight can
    # contribute its portion of the requested business day.
    activities=store.list_attendance_activity(tenant_id,shop_id,crm_user_id,
                                               start-timedelta(days=1),end)
    required=int((policy or {}).get("requiredWorkingMinutes",540))
    return {"userId":crm_user_id,"date":day.isoformat(),"timezone":zone,
            "summary":calculate_working_time(activities,required_minutes=required,day=day,
                                               timezone_name=zone),
            "note":"Open sessions are provisional; absence and paid-break treatment is not deducted without approved payroll policy"}


@app.get("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/activities/{activity_id}/evidence")
def integration_person_attendance_evidence(tenant_id: str, shop_id: str, crm_user_id: str,
                                           activity_id: str, request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    activity=store.get_person_attendance_activity(tenant_id,shop_id,crm_user_id,activity_id)
    if activity is None:
        raise HTTPException(404,"Attendance activity not found")
    evidence=activity.get("evidence") or {}
    # Never return unvalidated filesystem paths or storage keys as public URLs.
    return {"activityId":activity_id,"status":"MANIFEST_ONLY",
            "evidenceAvailable":bool(evidence),"manifest":evidence,
            "mediaAccess":"NOT_IMPLEMENTED","note":"Signed media access pending"}



@app.get("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/alerts")
def integration_person_attendance_alerts(tenant_id: str, shop_id: str, crm_user_id: str,
                                         request: Request, day: date, limit: int = 100):
    _require_crm_integration(request,tenant_id,shop_id)
    if not 1 <= limit <= 200:
        raise HTTPException(422,"Invalid limit")
    if not hasattr(store,"list_v2_absence_alerts"):
        raise HTTPException(503,"V2 alerts require PostgreSQL")
    return {"items":store.list_v2_absence_alerts(tenant_id,shop_id,crm_user_id,
                                                  day.isoformat(),limit),
            "date":day.isoformat(),"notificationDelivery":"NOT_IMPLEMENTED"}

@app.get("/integration/v1/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/daily-activity")
def integration_daily_activity(tenant_id: str, shop_id: str, crm_user_id: str, day: date, request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    raw_policy=store.attendance_policy(tenant_id,shop_id)
    zone=ZoneInfo(str(raw_policy.get("timezone") or "Asia/Kolkata"))
    start=datetime.combine(day,time.min,tzinfo=zone).astimezone(timezone.utc)
    end=(datetime.combine(day,time.min,tzinfo=zone)+timedelta(days=1)).astimezone(timezone.utc)
    items=store.list_attendance_activity(tenant_id,shop_id,crm_user_id,start,end)
    return {"tenant_id":tenant_id,"shop_id":shop_id,"crm_user_id":crm_user_id,
            "date":day.isoformat(),"timezone":str(zone),"items":items}


def _notify_cloud_event(envelope: dict[str, Any]) -> None:
    event_type=str(envelope.get("event_type") or "")
    alert_types={
        "UNKNOWN_INCIDENT","UNKNOWN_INSIDE_ALERT","CROWD_ALERT","LONG_BREAK_ALERT",
        "AUTO_CHECK_OUT","MAX_LOGOFF_AUTO_CHECK_OUT","ATTENDANCE_POLICY_VIOLATION",
    }
    if event_type not in alert_types:
        return
    tenant_id=str(envelope.get("tenant_id") or "")
    shop_id=str(envelope.get("shop_id") or "")
    if not tenant_id or not shop_id or not hasattr(store,"attendance_policy"):
        return
    policy=store.attendance_policy(tenant_id,shop_id)
    payload=envelope.get("payload") or {}
    metadata=payload.get("metadata") or {}
    reason=str(metadata.get("reason") or metadata.get("reason_code") or event_type)
    when=str(envelope.get("event_time") or "")
    camera=str(envelope.get("camera_id") or payload.get("camera_id") or "-")
    body=(f"Camera Eye alert: {event_type}\nTenant: {tenant_id}\nShop: {shop_id}\n"
          f"Camera: {camera}\nTime: {when}\nReason: {reason}")
    email_result=notification_service.send_email(
        policy.get("email_recipients") or [],f"Camera Eye - {event_type}",body)
    whatsapp_results=notification_service.send_whatsapp_text(
        policy.get("whatsapp_recipients") or [],body)
    logger.info("ALERT_DELIVERY event_id=%s event_type=%s email=%s whatsapp_sent=%s whatsapp_total=%s",
        str(envelope.get("event_id") or ""),event_type,email_result.delivered,
        sum(1 for item in whatsapp_results if item.delivered),len(whatsapp_results))


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
    allowed = {"index.html", "system-status.html", "cameras.html", "personnel.html", "attendance.html", "live.html", "alerts.html"}
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


def _edge_inventory_camera(tenant_id: str, shop_id: str, edge_id: str, camera_id: str) -> dict[str, Any] | None:
    """Resolve a credential-free camera advertised by the edge heartbeat."""
    for edge in store.list_edges(tenant_id, shop_id=shop_id):
        if str(edge.get("edge_id") or "") != edge_id:
            continue
        for camera in (edge.get("status") or {}).get("cameras") or []:
            if str(camera.get("camera_id") or "") == camera_id:
                return {
                    **camera,
                    "tenant_id": tenant_id,
                    "shop_id": shop_id,
                    "site_id": edge.get("site_id") or shop_id,
                    "edge_id": edge_id,
                    "source_type": camera.get("source_type") or "rtsp",
                    "source_configured": True,
                    "source_is_edge_local": True,
                    "edge_inventory": True,
                }
    return None


def _portal_camera_lookup(tenant_id: str, shop_id: str, edge_id: str, camera_id: str) -> dict[str, Any] | None:
    camera = store.get_camera(tenant_id, shop_id, edge_id, camera_id)
    return camera or _edge_inventory_camera(tenant_id, shop_id, edge_id, camera_id)


@app.get("/portal/v1/tenants/{tenant_id}/cameras")
def portal_cameras(tenant_id: str, shop_id: str | None = None, edge_id: str | None = None, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    if shop_id and shop_id != principal.shop_id: raise HTTPException(403,"Portal session is not authorized for this shop")
    configured = [_portal_camera_view(camera) for camera in store.list_cameras(tenant_id, shop_id=principal.shop_id, edge_id=edge_id)]
    keyed = {(str(item.get("edge_id") or ""), str(item.get("camera_id") or "")): item for item in configured}
    # Local Camera Eye setup remains authoritative for RTSP credentials. Heartbeats
    # publish only safe metadata/status so CRM can immediately see locally-created cameras.
    for edge in store.list_edges(tenant_id, shop_id=principal.shop_id):
        eid = str(edge.get("edge_id") or "")
        if edge_id and eid != edge_id:
            continue
        for advertised in (edge.get("status") or {}).get("cameras") or []:
            cid = str(advertised.get("camera_id") or "")
            key = (eid, cid)
            if key in keyed:
                keyed[key].update({k: advertised.get(k) for k in ("online","state","last_frame_at","capture_fps","ai_fps","last_error")})
                continue
            keyed[key] = {
                **advertised,
                "tenant_id": tenant_id,
                "company_code": principal.company_code,
                "shop_id": principal.shop_id,
                "site_id": edge.get("site_id") or principal.shop_id,
                "edge_id": eid,
                "source_configured": True,
                "source_is_edge_local": True,
                "edge_inventory": True,
            }
    return {"items": list(keyed.values())}


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
    commands=store.claim_edge_commands(principal.tenant_id,principal.shop_id,principal.edge_id)
    for command in commands:
        if command.get("command_type") in {"LIVE_VIEW_START","LIVE_VIEW_STOP"}:
            request=command.get("request") or {}
            logger.info("[LIVE_VIEW] command pickup %s", json.dumps({"command_id":command["id"],
                "edge_id":principal.edge_id,"camera_id":request.get("camera_id"),"session_id":request.get("session_id")}))
    return {"items":commands}


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
    # Edge polling must remain cheap. CRM synchronization is TTL-cached and
    # single-flight; normal polls serve the already mirrored personnel immediately.
    _refresh_crm_personnel(str(principal.tenant_id), str(principal.shop_id))
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
    if event_type in {"ATTENDANCE_ENTRY","ATTENDANCE_EXIT"} and hasattr(store,"person_attendance_policy"):
        person_policy=store.person_attendance_policy(tenant_id,shop_id,str(mapping["crm_user_id"]))
        if person_policy and str(person_policy.get("attendanceMode") or "AUTO").upper()=="MANUAL":
            logger.info("CRM_ATTENDANCE_EVENT_SKIPPED person_id=%s event_type=%s reason=manual_mode",
                        local_person_id,event_type)
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
    crm_date=event_time.date().isoformat()
    crm_time=event_time.strftime("%H:%M:%S")
    crm_user_id=mapping["crm_user_id"]
    # Automatic CRM mutations are deliberately split. The customer has now
    # confirmed the LoginLogout contract for automatic login, while automatic
    # logout remains opt-in until its business rule is confirmed. Manual portal
    # CHECK_IN/CHECK_OUT/BREAK actions remain available independently.
    auto_login_enabled=os.getenv("SNAPKEY_CRM_AUTO_LOGIN_ENABLED",
        os.getenv("SNAPKEY_CRM_ATTENDANCE_ENABLED","0")).strip()=="1"
    auto_logout_enabled=os.getenv("SNAPKEY_CRM_AUTO_LOGOUT_ENABLED","0").strip()=="1"
    location="Camera Eye - "+str(envelope.get("site_id") or shop_id)
    if event_type=="ATTENDANCE_ENTRY":
        if not auto_login_enabled:
            return
        crm_client.login_logout({"userId":crm_user_id,"date":crm_date,
            "actualStartTime":crm_time,"actualOffTime":None,
            "loginLocation":location,"logoutLocation":None})
    elif event_type=="ATTENDANCE_EXIT":
        if not auto_logout_enabled:
            return
        crm_client.login_logout({"userId":crm_user_id,"date":crm_date,
            "actualStartTime":None,"actualOffTime":crm_time,
            "loginLocation":None,"logoutLocation":location})
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


def _attendance_session_id(person_id: str, when: datetime) -> str:
    return f"attendance-{person_id}-{when.astimezone(timezone.utc).date().isoformat()}"

def _has_attendance_entry_today(tenant_id: str, shop_id: str, person_id: str, when: datetime) -> bool:
    day=when.astimezone(timezone.utc).date()
    for event in store.list_events(tenant_id,event_type="ATTENDANCE_ENTRY",limit=500,shop_id=shop_id):
        payload=event.get("payload") or {}
        inner=payload.get("payload") if isinstance(payload.get("payload"),dict) else payload
        if str(inner.get("person_id") or "")!=person_id:
            continue
        # Only a CRM-confirmed attendance mutation may suppress another automatic
        # attempt. Older builds recorded ATTENDANCE_ENTRY immediately after face
        # authentication even when UserRoster/LoginLogout had never succeeded;
        # those legacy audit rows must not block the repaired flow.
        metadata=inner.get("metadata") or {}
        crm_operation=str(metadata.get("crm_operation") or "")
        crm_confirmed=crm_operation=="loginUsingFaceTenant+LoginLogout" or metadata.get("manual") is True
        if not crm_confirmed:
            continue
        stamp=event.get("event_time")
        dt=stamp if isinstance(stamp,datetime) else datetime.fromisoformat(str(stamp).replace("Z","+00:00"))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        if dt.astimezone(timezone.utc).date()==day:
            return True
    return False

def _crm_face_login_identity(tenant_code: str, crm_user_id: str) -> tuple[str, str]:
    """Return CRM tenant UUID and the user's enrolled face image from the cached directory.

    loginUsingFaceTenant must receive the enrolled Base64 image returned by CRM's
    face-embeddings directory, not Camera Eye recognition evidence. Preserve the CRM
    image bytes exactly: only remove an optional data-URL prefix.
    """
    raw=crm_client.face_embeddings(tenant_code)
    users=raw if isinstance(raw,list) else (raw.get("items") or raw.get("data") or [])
    target=str(crm_user_id or "").strip()
    for user in users:
        if not isinstance(user,dict) or str(user.get("id") or "").strip()!=target:
            continue
        crm_tenant_id=str(user.get("tenantId") or "").strip()
        if not crm_tenant_id:
            raise RuntimeError("CRM face directory user is missing tenantId")

        sources=_crm_image_sources(user)
        for source in sources:
            image=source.strip()
            if image.startswith(("http://","https://","/")):
                continue
            if image.startswith("data:") and "," in image:
                image=image.split(",",1)[1].strip()
            if image:
                logger.info(
                    "CRM_FACE_LOGIN_IDENTITY_RESOLVED tenant_code=%s crm_user_id=%s crm_tenant_id=%s enrolled_image_present=true",
                    tenant_code,target,crm_tenant_id,
                )
                return crm_tenant_id,image

        logger.error(
            "CRM_FACE_LOGIN_IMAGE_MISSING tenant_code=%s crm_user_id=%s image_source_count=%s",
            tenant_code,target,len(sources),
        )
        raise RuntimeError("CRM face directory did not return an enrolled Base64 face image for the requested CRM user")

    logger.error("CRM_TENANT_RESOLUTION_FAILED tenant_code=%s crm_user_id=%s directory_users=%s",
                 tenant_code,target,len(users))
    raise RuntimeError("CRM face directory did not return the requested CRM user")


def _crm_tenant_uuid_for_user(tenant_code: str, crm_user_id: str) -> str:
    """Resolve CRM's tenant UUID from the authoritative face directory."""
    raw=crm_client.face_embeddings(tenant_code)
    users=raw if isinstance(raw,list) else (raw.get("items") or raw.get("data") or [])
    target=str(crm_user_id or "").strip()
    for user in users:
        if not isinstance(user,dict):
            continue
        candidate_user=str(user.get("id") or "").strip()
        candidate_tenant=str(user.get("tenantId") or "").strip()
        if candidate_user==target and candidate_tenant:
            logger.info("CRM_TENANT_RESOLVED tenant_code=%s crm_user_id=%s crm_tenant_id=%s",
                        tenant_code,target,candidate_tenant)
            return candidate_tenant
    logger.error("CRM_TENANT_RESOLUTION_FAILED tenant_code=%s crm_user_id=%s directory_users=%s",
                 tenant_code,target,len(users))
    raise RuntimeError("CRM face directory did not return tenantId for the requested CRM user")

def _crm_allowed_user_ids(tenant_code: str) -> set[str]:
    """Return CRM user ids explicitly belonging to the requested tenant code."""
    raw=crm_client.face_embeddings(tenant_code)
    users=raw if isinstance(raw,list) else (raw.get("items") or raw.get("data") or [])
    ids={str(user.get("id") or "").strip() for user in users
         if isinstance(user,dict) and str(user.get("id") or "").strip()}
    logger.info("CRM_TENANT_DIRECTORY tenant_code=%s user_count=%s",tenant_code,len(ids))
    return ids

def _crm_roster_user_ids(rows: Any) -> set[str]:
    if not isinstance(rows,list):
        return set()
    return {str(row.get("userId") or "").strip() for row in rows
            if isinstance(row,dict) and str(row.get("userId") or "").strip()}

def _assert_crm_service_token_scope(tenant_code: str) -> None:
    """Fail closed when the static CRM token demonstrably belongs to another tenant."""
    allowed=_crm_allowed_user_ids(tenant_code)
    now=datetime.now(timezone.utc)
    rows=crm_client.users_roster(now.year,now.month)
    roster_ids=_crm_roster_user_ids(rows)
    if allowed and roster_ids and not (allowed & roster_ids):
        logger.error(
            "CRM_TENANT_SCOPE_MISMATCH tenant_code=%s directory_users=%s roster_users=%s overlap=0",
            tenant_code,len(allowed),len(roster_ids),
        )
        raise RuntimeError("Configured CRM service token is scoped to a different tenant")

def _recognition_image_base64(payload: dict[str, Any]) -> str:
    """Load uploaded recognition evidence and return raw JPEG Base64."""
    metadata=payload.get("metadata") or {}
    evidence=metadata.get("cloud_evidence") or {}
    evidence_id=str(evidence.get("evidence_id") or "").strip() if isinstance(evidence,dict) else ""
    if not evidence_id:
        raise RuntimeError("Recognition event has no uploaded face evidence")
    root=Path(os.getenv("SNAPKEY_EVIDENCE_ROOT","/app/data/evidence")).resolve()
    target=(root/evidence_id).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise RuntimeError("Recognition face evidence is unavailable")
    data=target.read_bytes()
    suffix=target.suffix.lower()
    if suffix not in {".jpg",".jpeg"}:
        image=cv2.imdecode(np.frombuffer(data,np.uint8),cv2.IMREAD_COLOR)
        if image is None:
            raise RuntimeError("Recognition face evidence is not a valid image")
        ok,encoded=cv2.imencode(".jpg",image,[int(cv2.IMWRITE_JPEG_QUALITY),90])
        if not ok:
            raise RuntimeError("Unable to encode recognition face evidence as JPEG")
        data=encoded.tobytes()
    return base64.b64encode(data).decode("ascii")

def _crm_face_login_succeeded(result: Any) -> bool:
    return crm_client.business_success(result)

def _crm_mutation_succeeded(result: Any) -> bool:
    return crm_client.business_success(result)

def _auto_attend_recognized_person(envelope: dict[str, Any]) -> None:
    """Immediately face-login a recognized person from an entrance camera."""
    event_id=str(envelope.get("event_id") or "")
    event_type=str(envelope.get("event_type") or "")
    tenant_id=str(envelope.get("tenant_id") or "")
    shop_id=str(envelope.get("shop_id") or "")
    edge_id=str(envelope.get("edge_id") or "")
    camera_id=str(envelope.get("camera_id") or "")
    if event_type!="PERSON_RECOGNIZED":
        logger.info("AUTO_ATTENDANCE_SKIPPED event_id=%s reason=event_type event_type=%s",event_id,event_type)
        return
    if not crm_client.face_attendance_configured:
        logger.error("AUTO_ATTENDANCE_SKIPPED event_id=%s tenant_code=%s camera_id=%s reason=crm_face_attendance_not_configured",
                     event_id,tenant_id,camera_id)
        return

    # Edge envelopes intentionally wrap the local event payload:
    # envelope.payload = { ..., "payload": {person_id, metadata, ...}, ... }.
    # Older edge versions may send the local payload directly, so support both.
    outer_payload=envelope.get("payload") or {}
    nested_payload=outer_payload.get("payload") if isinstance(outer_payload,dict) else None
    payload=nested_payload if isinstance(nested_payload,dict) else outer_payload
    person_id=str(payload.get("person_id") or "").strip() if isinstance(payload,dict) else ""
    logger.info(
        "AUTO_ATTENDANCE_RECEIVED event_id=%s tenant_code=%s shop_id=%s edge_id=%s camera_id=%s person_id=%s payload_shape=%s",
        event_id,tenant_id,shop_id,edge_id,camera_id,person_id or "-",
        "nested" if isinstance(nested_payload,dict) else "direct",
    )
    if not person_id:
        safe_keys=sorted(str(k) for k in payload.keys()) if isinstance(payload,dict) else []
        logger.error(
            "AUTO_ATTENDANCE_SKIPPED event_id=%s tenant_code=%s camera_id=%s reason=missing_person_id payload_keys=%s",
            event_id,tenant_id,camera_id,safe_keys,
        )
        return
    camera=_portal_camera_lookup(tenant_id,shop_id,edge_id,camera_id)
    if not camera or str(camera.get("camera_role") or "").upper()!="ENTRANCE_EXIT":
        logger.warning("AUTO_ATTENDANCE_SKIPPED event_id=%s person_id=%s camera_id=%s reason=not_attendance_camera role=%s", event_id,person_id,camera_id,str(camera.get("camera_role") if camera else "camera_not_found"))
        return
    mapping=store.crm_person_mapping(tenant_id,shop_id,person_id)
    if not mapping:
        logger.warning("CRM_FACE_LOGIN_SKIPPED person_id=%s reason=no_crm_mapping",person_id)
        return
    stamp=envelope.get("event_time")
    when=datetime.fromisoformat(str(stamp).replace("Z","+00:00"))
    if when.tzinfo is None:
        when=when.replace(tzinfo=timezone.utc)
    when=when.astimezone(timezone.utc)
    if hasattr(store,"touch_attendance_presence"):
        store.touch_attendance_presence(
            tenant_id=tenant_id,shop_id=shop_id,local_person_id=person_id,
            crm_user_id=str(mapping["crm_user_id"]),seen_at=when,camera_id=camera_id,
            recognition_event_id=event_id,checked_in=None,
        )
    if hasattr(store,"person_attendance_policy"):
        person_policy=store.person_attendance_policy(tenant_id,shop_id,str(mapping["crm_user_id"]))
        if person_policy and str(person_policy.get("attendanceMode") or "AUTO").upper()=="MANUAL":
            logger.info("AUTO_ATTENDANCE_SKIPPED event_id=%s person_id=%s reason=manual_mode",event_id,person_id)
            return
    # This is duplicate protection, not a recognition debounce: the first valid
    # recognition is sent to CRM immediately, then further successful logins for
    # the same person/day are suppressed.
    if _has_attendance_entry_today(tenant_id,shop_id,person_id,when):
        logger.warning("AUTO_ATTENDANCE_SKIPPED event_id=%s person_id=%s camera_id=%s reason=attendance_already_exists", event_id,person_id,camera_id)
        return
    try:
        crm_tenant_id,enrolled_face_base64=_crm_face_login_identity(
            tenant_id,mapping["crm_user_id"]
        )
        logger.info("CRM_FACE_LOGIN_ATTEMPT person_id=%s crm_user_id=%s camera_id=%s tenant_id=%s image_source=crm_enrolled_face",
            person_id,mapping["crm_user_id"],camera_id,crm_tenant_id)
        crm_result=crm_client.login_using_face_tenant(enrolled_face_base64,crm_tenant_id)
        if not _crm_face_login_succeeded(crm_result):
            logger.warning("CRM_FACE_LOGIN_REJECTED person_id=%s crm_user_id=%s",
                person_id,mapping["crm_user_id"])
            return

        # Match the supplied n8n workflow exactly: loginUsingFaceTenant identifies
        # the person and returns a short-lived CRM token; that returned token is
        # then used as the Authorization header for UserRoster/LoginLogout.
        if not isinstance(crm_result,dict):
            raise RuntimeError("CRM face login returned an unexpected response")
        face_token=str(crm_result.get("token") or "").strip()
        crm_user=crm_result.get("user") if isinstance(crm_result.get("user"),dict) else {}
        authenticated_user_id=str(crm_user.get("id") or "").strip()
        if not face_token:
            raise RuntimeError("CRM face login succeeded without returning token")
        if not authenticated_user_id:
            raise RuntimeError("CRM face login succeeded without returning user.id")
        expected_user_id=str(mapping.get("crm_user_id") or "").strip()
        if expected_user_id and authenticated_user_id!=expected_user_id:
            logger.warning("CRM_FACE_IDENTITY_MISMATCH person_id=%s expected_crm_user_id=%s authenticated_crm_user_id=%s",
                person_id,expected_user_id,authenticated_user_id)
            return

        crm_date=when.isoformat(timespec="milliseconds").replace("+00:00","Z")
        crm_time=when.strftime("%H:%M:%S")
        attendance_result=crm_client.login_logout_with_face_token({
            "userId":authenticated_user_id,
            "date":crm_date,
            "actualStartTime":crm_time,
        },face_token)
        if not _crm_mutation_succeeded(attendance_result):
            logger.error(
                "CRM_AUTO_ATTENDANCE_REJECTED person_id=%s crm_user_id=%s camera_id=%s message=%s",
                person_id,authenticated_user_id,camera_id,
                str(attendance_result.get("message") or "")[:240] if isinstance(attendance_result,dict) else "",
            )
            return
    except Exception:
        # Never log the Base64 face image or the face-login token.
        logger.exception("CRM_FACE_LOGIN_FAILED person_id=%s crm_user_id=%s camera_id=%s",
            person_id,mapping["crm_user_id"],camera_id)
        return
    logger.info("CRM_AUTO_ATTENDANCE_SUCCESS person_id=%s crm_user_id=%s camera_id=%s",
        person_id,authenticated_user_id,camera_id)
    if hasattr(store,"touch_attendance_presence"):
        store.touch_attendance_presence(
            tenant_id=tenant_id,shop_id=shop_id,local_person_id=person_id,
            crm_user_id=authenticated_user_id,seen_at=when,camera_id=camera_id,
            recognition_event_id=str(envelope.get("event_id") or ""),checked_in=True,
        )
        store.record_attendance_activity({
            "id":"activity-"+secrets.token_urlsafe(12),"tenant_id":tenant_id,"shop_id":shop_id,
            "crm_user_id":authenticated_user_id,"local_person_id":person_id,
            "activity_type":"CHECK_IN","occurred_at":when,"source":"CAMERA_EYE",
            "camera_id":camera_id,"reason_code":"FACE_RECOGNITION",
            "metadata":{"recognition_event_id":str(envelope.get("event_id") or "")},
        })
    event_id="auto-attendance-"+secrets.token_urlsafe(12)
    store.record_portal_event({"event_id":event_id,"tenant_id":tenant_id,
        "company_code":envelope.get("company_code"),"shop_id":shop_id,
        "site_id":str(envelope.get("site_id") or shop_id),"edge_id":edge_id,
        "camera_id":camera_id,"event_type":"ATTENDANCE_ENTRY",
        "event_time":when.isoformat(timespec="milliseconds").replace("+00:00","Z"),
        "payload":{"person_id":person_id,"event_type":"ATTENDANCE_ENTRY",
        "metadata":{"attendance_session_id":_attendance_session_id(person_id,when),
        "recognition_event_id":envelope.get("event_id"),"automatic":True,
        "crm_operation":"loginUsingFaceTenant+LoginLogout","crm_tenant_id":crm_tenant_id,
        "crm_user_id":authenticated_user_id}}})

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
        # Alert delivery is cloud-side and best-effort; edge event ingestion remains
        # durable even when SMTP or WhatsApp providers are temporarily unavailable.
        background_tasks.add_task(_notify_cloud_event,envelope)
        if str(envelope.get("event_type") or "")=="PERSON_RECOGNIZED":
            logger.warning(
                "AUTO_ATTENDANCE_QUEUED event_id=%s tenant_code=%s shop_id=%s edge_id=%s camera_id=%s",
                str(envelope.get("event_id") or ""),str(envelope.get("tenant_id") or ""),
                str(envelope.get("shop_id") or ""),str(envelope.get("edge_id") or ""),
                str(envelope.get("camera_id") or ""),
            )
            background_tasks.add_task(_auto_attend_recognized_person,envelope)
        else:
            background_tasks.add_task(_deliver_crm_attendance_event,envelope)
    else:
        logger.info("EDGE_EVENT_DUPLICATE event_id=%s event_type=%s",
                    str(envelope.get("event_id") or ""),str(envelope.get("event_type") or ""))
    return result


@app.delete("/portal/v1/tenants/{tenant_id}/edges/{edge_id}")
def delete_portal_edge(tenant_id: str, edge_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id,principal)
    edge=next((item for item in store.list_edges(tenant_id,shop_id=principal.shop_id)
               if str(item.get("edge_id") or "")==edge_id),None)
    if not edge:
        raise HTTPException(404,"Edge device not found")
    stamp=edge.get("received_at") or edge.get("last_seen_at")
    if stamp:
        seen=datetime.fromisoformat(str(stamp).replace("Z","+00:00"))
        if seen.tzinfo is None: seen=seen.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc)-seen).total_seconds()<90:
            raise HTTPException(409,"Online edge devices cannot be removed. Stop the edge service first and wait until it is offline.")
    if not store.delete_edge(tenant_id,principal.shop_id,edge_id):
        raise HTTPException(404,"Edge device not found")
    return {"deleted":True,"edge_id":edge_id,"credentials_revoked":True}


def _crm_embedding_vector(value: Any) -> list[float] | None:
    """Extract a numeric face vector from CRM's list or object-shaped embedding payloads."""
    if isinstance(value, list):
        if value and all(isinstance(x, (int, float)) and not isinstance(x, bool) for x in value):
            return [float(x) for x in value]
        for item in value:
            vector = _crm_embedding_vector(item)
            if vector:
                return vector
        return None
    if isinstance(value, dict):
        # CRM versions have returned wrapper objects instead of a bare vector.
        for key in ("embedding", "faceEmbedding", "vector", "values", "data"):
            if key in value:
                vector = _crm_embedding_vector(value.get(key))
                if vector:
                    return vector
        # Last-resort traversal keeps this forward compatible without depending on
        # metadata field names; only a 512-D numeric vector is accepted by caller.
        for item in value.values():
            vector = _crm_embedding_vector(item)
            if vector:
                return vector
    return None


def _crm_image_sources(user: dict[str, Any]) -> list[str]:
    """Normalize CRM faceImages/profileImage values without logging biometric data."""
    sources: list[str] = []
    for value in (user.get("faceImages"), user.get("profileImage")):
        if isinstance(value, str):
            if value.strip():
                sources.append(value.strip())
        elif isinstance(value, list):
            sources.extend(item.strip() for item in value if isinstance(item, str) and item.strip())
    return sources


def _decode_crm_face_image(source: str) -> bytes | None:
    """Decode data-URL or raw Base64 CRM face images; remote paths remain display-only."""
    import base64
    value = source.strip()
    if not value:
        return None
    if value.startswith("data:image/"):
        if "," not in value:
            return None
        value = value.split(",", 1)[1]
    elif value.startswith(("http://", "https://", "/")):
        return None
    try:
        return base64.b64decode(value, validate=False)
    except Exception:
        return None


def _refresh_crm_personnel(tenant_id: str, shop_id: str, *, force: bool = False) -> None:
    """Refresh CRM personnel at most once per tenant/shop TTL.

    Edge polling reads the local mirror. A per-tenant/shop single-flight lock prevents
    concurrent edge requests from repeating CRM fetches or CPU-heavy face enrollment.
    """
    if not crm_client.face_attendance_configured:
        raise HTTPException(503,"SnapKey CRM face directory is not configured")
    if not force and not _crm_personnel_refresh_due(tenant_id,shop_id):
        return
    refresh_lock=_crm_personnel_refresh_lock(tenant_id,shop_id)
    with refresh_lock:
        if not force and not _crm_personnel_refresh_due(tenant_id,shop_id):
            return
        _refresh_crm_personnel_locked(tenant_id,shop_id)
        _crm_personnel_last_refresh[(tenant_id,shop_id)]=monotonic_time.monotonic()

def _refresh_crm_personnel_locked(tenant_id: str, shop_id: str) -> None:
    try:
        raw=crm_client.face_embeddings(tenant_id)
    except httpx.HTTPError as exc:
        raise HTTPException(502,f"SnapKey CRM personnel lookup failed: {exc}") from exc
    users=raw if isinstance(raw,list) else (raw.get("items") or raw.get("data") or [])
    seen_local_ids: set[str] = set()
    for user in users:
        if not isinstance(user,dict) or not str(user.get("id") or "").strip():
            continue
        crm_user_id=str(user["id"])
        name=str(user.get("name") or user.get("userName") or crm_user_id)
        role=str(user.get("roleName") or ("ADMIN" if user.get("isAdmin") else "WORKER")).upper()
        employee_code=str(user.get("employeeCode") or user.get("userName") or crm_user_id)

        # Preserve an existing Camera Eye id for historical attendance/events when the
        # employee code matches, but always update it from the current CRM identity.
        existing_person=next((p for p in store.list_cloud_people(tenant_id,shop_id)
                              if str(p.get("employee_code") or "").strip().lower()==employee_code.strip().lower()),None)
        local_person_id=str(existing_person["id"]) if existing_person else crm_user_id
        seen_local_ids.add(local_person_id)
        if existing_person:
            store.update_cloud_person(tenant_id,shop_id,local_person_id,{
                "full_name":name,"role":role,"phone":user.get("phoneNumber"),"email":user.get("email"),
                "active":bool(user.get("isActive",True)),
            })
        else:
            store.upsert_crm_cloud_person({
                "id":local_person_id,"tenant_id":tenant_id,"shop_id":shop_id,
                "employee_code":employee_code,"full_name":name,"role":role,
                "phone":user.get("phoneNumber"),"email":user.get("email"),
                "active":bool(user.get("isActive",True)),
            })
        store.upsert_crm_person_mapping({"tenant_id":tenant_id,"shop_id":shop_id,"local_person_id":local_person_id,
            "crm_user_id":crm_user_id,"employee_code":employee_code})

        existing=store.list_cloud_faces(tenant_id,shop_id,local_person_id,include_embedding=True)
        existing_vectors={json.dumps(face.get("embedding") or [],separators=(",",":")) for face in existing}

        # CRM currently returns faceEmbeddings as wrapper objects on some tenants.
        # Extract only InsightFace-compatible 512-D numeric vectors.
        raw_vectors=user.get("faceEmbeddings") or []
        candidates=raw_vectors if isinstance(raw_vectors,list) else [raw_vectors]
        if isinstance(raw_vectors,list) and raw_vectors and all(isinstance(x,(int,float)) and not isinstance(x,bool) for x in raw_vectors):
            candidates=[raw_vectors]
        for candidate in candidates:
            vector=_crm_embedding_vector(candidate)
            if not vector or len(vector)!=512:
                continue
            key=json.dumps(vector,separators=(",",":"))
            if key in existing_vectors:
                continue
            store.add_cloud_face({"id":secrets.token_urlsafe(18),"person_id":local_person_id,
                "tenant_id":tenant_id,"shop_id":shop_id,"embedding":vector,"quality":1.0,"image_path":None})
            existing_vectors.add(key)

        # Also enroll CRM face images with Camera Eye's own InsightFace model. This is
        # intentionally done even when CRM supplied vectors exist: CRM vector wrappers
        # may come from a different model/version, while image-derived embeddings are
        # guaranteed to match the recognizer running on the edge.
        for source in _crm_image_sources(user):
            raw_image=_decode_crm_face_image(source)
            if not raw_image:
                continue
            # Never run InsightFace repeatedly for an unchanged CRM image. The
            # fingerprint intentionally contains no biometric bytes and is scoped
            # by tenant/shop/person.
            source_sha=hashlib.sha256(raw_image).hexdigest()
            fingerprint=(tenant_id,shop_id,local_person_id,source_sha)
            if fingerprint in _crm_face_image_fingerprints:
                continue
            try:
                image=cv2.imdecode(np.frombuffer(raw_image,np.uint8),cv2.IMREAD_COLOR)
                if image is None:
                    _crm_face_image_fingerprints.add(fingerprint)
                    continue
                embedding,quality=_cloud_face_enroller().enroll(image)
                if len(embedding)!=512:
                    continue
                key=json.dumps(embedding,separators=(",",":"))
                if key not in existing_vectors:
                    store.add_cloud_face({"id":secrets.token_urlsafe(18),"person_id":local_person_id,
                        "tenant_id":tenant_id,"shop_id":shop_id,"embedding":embedding,
                        "quality":quality,"image_path":None})
                    existing_vectors.add(key)
                _crm_face_image_fingerprints.add(fingerprint)
            except Exception:
                logger.exception("CRM_FACE_ENROLL_FAILED tenant_id=%s shop_id=%s person_id=%s",
                                 tenant_id,shop_id,local_person_id)

    # CRM is authoritative for lifecycle as well. People that disappear from the
    # current CRM face directory must not remain active recognition candidates.
    for person in store.list_cloud_people(tenant_id,shop_id):
        pid=str(person["id"])
        if pid not in seen_local_ids and bool(person.get("active")):
            store.update_cloud_person(tenant_id,shop_id,pid,{"active":False})

@app.get("/portal/v1/tenants/{tenant_id}/personnel")
def portal_personnel(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    _refresh_crm_personnel(tenant_id,principal.shop_id)
    edge_people={}
    for edge in store.list_edges(tenant_id,shop_id=principal.shop_id):
        for person in (edge.get("status") or {}).get("personnel") or []:
            edge_people.setdefault(str(person.get("person_id") or ""),[]).append(str(edge.get("edge_id") or ""))
    items=[]
    for person in store.list_cloud_people(tenant_id,principal.shop_id):
        pid=str(person["id"]); item=dict(person)
        item["person_id"]=pid; item["edge_ids"]=sorted(set(edge_people.get(pid,[])))
        item["edge_synced"]=bool(item["edge_ids"]); item["face_enrolled"]=int(item.get("face_count") or 0)>0
        item["crm_user_id"]=pid
        items.append(item)
    return {"items":items}

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

def _livekit_token(room: str, identity: str, *, publish: bool, subscribe: bool, ttl_seconds: int) -> str:
    try:
        from livekit import api as livekit_api
    except ImportError as exc:
        raise HTTPException(503, "LiveKit server SDK is not installed") from exc
    api_key=os.getenv("SNAPKEY_LIVEKIT_API_KEY","").strip()
    api_secret=os.getenv("SNAPKEY_LIVEKIT_API_SECRET","").strip()
    if not api_key or not api_secret:
        raise HTTPException(503, "Attendance live view is not configured")
    return (livekit_api.AccessToken(api_key,api_secret)
        .with_identity(identity)
        .with_grants(livekit_api.VideoGrants(room_join=True,room=room,can_publish=publish,can_subscribe=subscribe,can_publish_data=False))
        .with_ttl(timedelta(seconds=ttl_seconds+60))
        .to_jwt())


def _create_live_view_session(tenant_id: str, request: AttendanceLiveStartRequest, principal: PortalPrincipal, *, attendance_only: bool) -> dict[str, Any]:
    """Create both grants and the edge command for the same scoped room."""
    _portal_scope(tenant_id,principal)
    camera=_portal_camera_lookup(tenant_id,principal.shop_id,request.edge_id,request.camera_id)
    if not camera:
        raise HTTPException(404,"Camera not found")
    if attendance_only and str(camera.get("camera_role") or "").upper()!="ENTRANCE_EXIT":
        raise HTTPException(400,"Remote live view is available only for attendance/entrance cameras")
    livekit_url=os.getenv("SNAPKEY_LIVEKIT_URL","").strip()
    if not livekit_url:
        raise HTTPException(503,"Attendance live view is not configured")
    logger.info("[LIVE_VIEW] start requested %s", json.dumps({"tenant_id":tenant_id,"shop_id":principal.shop_id,
        "camera_id":request.camera_id,"edge_id":request.edge_id}))
    session_id=secrets.token_urlsafe(18)
    room="camera-eye-"+secrets.token_urlsafe(18)
    publisher_token=_livekit_token(room,"edge-"+secrets.token_urlsafe(12),publish=True,subscribe=False,ttl_seconds=request.ttl_seconds)
    viewer_token=_livekit_token(room,"viewer-"+secrets.token_urlsafe(12),publish=False,subscribe=True,ttl_seconds=request.ttl_seconds)
    command=store.create_edge_command({"tenant_id":tenant_id,"shop_id":principal.shop_id,"edge_id":request.edge_id,
        "command_type":"LIVE_VIEW_START","request":{"session_id":session_id,"camera_id":request.camera_id,"url":livekit_url,
        "room":room,"publisher_token":publisher_token,"ttl_seconds":request.ttl_seconds}})
    logger.info("[LIVE_VIEW] session created %s", json.dumps({"session_id":session_id,"room":room,
        "edge_id":request.edge_id,"camera_id":request.camera_id,"command_id":command.get("id")}))
    return {"session_id":session_id,"room":room,
        "expires_at":(datetime.now(timezone.utc)+timedelta(seconds=request.ttl_seconds+60)).isoformat(),
        "camera_id":request.camera_id,"edge_id":request.edge_id,"url":livekit_url,
        "viewer_token":viewer_token,"ttl_seconds":request.ttl_seconds,"command_id":command.get("id"),"transport":"webrtc"}


@app.post("/portal/v1/tenants/{tenant_id}/attendance-station/live/start")
def attendance_station_live_start(tenant_id: str, request: AttendanceLiveStartRequest, principal: PortalPrincipal = Depends(require_portal_session)):
    return _create_live_view_session(tenant_id,request,principal,attendance_only=True)


@app.post("/portal/v1/tenants/{tenant_id}/attendance-station/live/stop")
def attendance_station_live_stop(tenant_id: str, request: AttendanceLiveStopRequest, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    camera=_portal_camera_lookup(tenant_id,principal.shop_id,request.edge_id,request.camera_id)
    if not camera:
        raise HTTPException(404,"Camera not found")
    command=store.create_edge_command({"tenant_id":tenant_id,"shop_id":principal.shop_id,"edge_id":request.edge_id,
        "command_type":"LIVE_VIEW_STOP","request":{"session_id":request.session_id,"camera_id":request.camera_id}})
    logger.info("[LIVE_VIEW] stop requested %s", json.dumps({"session_id":request.session_id,
        "edge_id":request.edge_id,"camera_id":request.camera_id,"command_id":command.get("id")}))
    return {"ok":True,"session_id":request.session_id,"command_id":command.get("id")}

@app.post("/portal/v1/tenants/{tenant_id}/live/start")
def portal_live_start(tenant_id: str, request: AttendanceLiveStartRequest, principal: PortalPrincipal = Depends(require_portal_session)):
    """Start an on-demand AI-annotated stream for any online Camera Eye camera."""
    return _create_live_view_session(tenant_id,request,principal,attendance_only=False)

@app.post("/portal/v1/tenants/{tenant_id}/live/stop")
def portal_live_stop(tenant_id: str, request: AttendanceLiveStopRequest, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    camera=_portal_camera_lookup(tenant_id,principal.shop_id,request.edge_id,request.camera_id)
    if not camera:
        raise HTTPException(404,"Camera not found")
    command=store.create_edge_command({"tenant_id":tenant_id,"shop_id":principal.shop_id,"edge_id":request.edge_id,
        "command_type":"LIVE_VIEW_STOP","request":{"session_id":request.session_id,"camera_id":request.camera_id}})
    logger.info("[LIVE_VIEW] stop requested %s", json.dumps({"session_id":request.session_id,
        "edge_id":request.edge_id,"camera_id":request.camera_id,"command_id":command.get("id")}))
    return {"ok":True,"session_id":request.session_id,"command_id":command.get("id")}


@app.get("/portal/v1/tenants/{tenant_id}/attendance-station/candidate")
def attendance_station_candidate(tenant_id: str, camera_id: str, edge_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    camera=_portal_camera_lookup(tenant_id,principal.shop_id,edge_id,camera_id)
    if not camera or str(camera.get("camera_role") or "").upper()!="ENTRANCE_EXIT":
        raise HTTPException(400,"Attendance Station is available only for attendance/entrance cameras")
    now=datetime.now(timezone.utc)
    for event in store.list_events(tenant_id,event_type="PERSON_RECOGNIZED",limit=50,shop_id=principal.shop_id):
        if str(event.get("camera_id") or "")!=camera_id or str(event.get("edge_id") or "")!=edge_id:
            continue
        stamp=event.get("event_time"); dt=stamp if isinstance(stamp,datetime) else datetime.fromisoformat(str(stamp).replace("Z","+00:00"))
        if dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)
        age=(now-dt.astimezone(timezone.utc)).total_seconds()
        if age>15: break
        envelope=event.get("payload") or {}; payload=envelope.get("payload") if isinstance(envelope.get("payload"),dict) else envelope
        person_id=str(payload.get("person_id") or "").strip(); metadata=payload.get("metadata") or {}
        person=store.get_cloud_person(tenant_id,principal.shop_id,person_id) if person_id else None
        mapping=store.crm_person_mapping(tenant_id,principal.shop_id,person_id) if person_id else None
        return {"candidate":{"recognition_event_id":event.get("id"),"person_id":person_id,"full_name":(person or {}).get("full_name") or person_id,
            "employee_code":(person or {}).get("employee_code"),"role":(person or {}).get("role"),"confidence":metadata.get("confidence"),
            "camera_id":camera_id,"edge_id":edge_id,"detected_at":str(event.get("event_time")),"expires_in_seconds":max(0,int(15-age)),
            "crm_mapped":bool(mapping),"break_configured":bool(mapping and mapping.get("break_master_id"))}}
    return {"candidate":None}

@app.post("/portal/v1/tenants/{tenant_id}/attendance-station/action")
def attendance_station_action(tenant_id: str, request: AttendanceStationActionRequest, principal: PortalPrincipal = Depends(require_portal_session)):
    """Execute a CRM-backed manual action for the current recognized person.

    Every manual action first authenticates the current camera face using
    loginUsingFaceTenant (base64Image + tenantId, no static CRM JWT). The short-lived
    token returned by CRM is then used for LoginLogout or the break mutation. This
    keeps CHECK_IN, CHECK_OUT, BREAK_START and BREAK_END scoped to the person who is
    physically present at the attendance camera.
    """
    _portal_scope(tenant_id,principal)
    action=request.action.strip().upper()
    if action not in {"CHECK_IN","CHECK_OUT","BREAK_START","BREAK_END"}:
        raise HTTPException(400,"Unsupported attendance action")
    camera=_portal_camera_lookup(tenant_id,principal.shop_id,request.edge_id,request.camera_id)
    if not camera or str(camera.get("camera_role") or "").upper()!="ENTRANCE_EXIT":
        raise HTTPException(400,"Manual attendance actions require an attendance/entrance camera")
    event=store.get_event(tenant_id,principal.shop_id,request.recognition_event_id)
    if not event or str(event.get("event_type") or "")!="PERSON_RECOGNIZED" or str(event.get("camera_id") or "")!=request.camera_id or str(event.get("edge_id") or "")!=request.edge_id:
        raise HTTPException(409,"Recognition candidate is no longer valid")
    stamp=event.get("event_time"); detected=stamp if isinstance(stamp,datetime) else datetime.fromisoformat(str(stamp).replace("Z","+00:00"))
    if detected.tzinfo is None: detected=detected.replace(tzinfo=timezone.utc)
    if (datetime.now(timezone.utc)-detected.astimezone(timezone.utc)).total_seconds()>15:
        raise HTTPException(409,"Recognition expired; face the attendance camera again")
    envelope=event.get("payload") or {}; payload=envelope.get("payload") if isinstance(envelope.get("payload"),dict) else envelope
    person_id=str(payload.get("person_id") or "").strip(); mapping=store.crm_person_mapping(tenant_id,principal.shop_id,person_id) if person_id else None
    if not mapping: raise HTTPException(409,"Recognized person is not mapped to a CRM user")
    if not crm_client.face_attendance_configured:
        raise HTTPException(503,"SnapKey CRM face attendance is not configured")

    now=datetime.now(timezone.utc); crm_timestamp=now.isoformat(timespec="milliseconds").replace("+00:00","Z")
    crm_date=crm_timestamp; crm_time=now.strftime("%H:%M:%S")
    authenticated_user_id=str(mapping["crm_user_id"])
    try:
        # Authenticate the live recognized face for all four manual actions. CRM's
        # face-login endpoint is public by contract and returns the short-lived token
        # required by the subsequent attendance/break mutation.
        crm_tenant_id=_crm_tenant_uuid_for_user(tenant_id,mapping["crm_user_id"])
        image_base64=_recognition_image_base64(payload)
        face_result=crm_client.login_using_face_tenant(image_base64,crm_tenant_id)
        if not _crm_face_login_succeeded(face_result) or not isinstance(face_result,dict):
            raise HTTPException(409,"CRM face authentication was rejected")
        face_token=str(face_result.get("token") or "").strip()
        crm_user=face_result.get("user") if isinstance(face_result.get("user"),dict) else {}
        authenticated_user_id=str(crm_user.get("id") or "").strip()
        if not face_token or not authenticated_user_id:
            raise HTTPException(502,"CRM face authentication did not return token and user identity")
        if authenticated_user_id!=str(mapping["crm_user_id"]):
            raise HTTPException(409,"CRM face identity does not match the recognized Camera Eye person")

        if action=="CHECK_IN":
            result=crm_client.login_logout_with_face_token({
                "userId":authenticated_user_id,"date":crm_date,"actualStartTime":crm_time,
            },face_token)
        elif action=="CHECK_OUT":
            result=crm_client.login_logout_with_face_token({
                "userId":authenticated_user_id,"date":crm_date,"actualOffTime":crm_time,
            },face_token)
        elif action=="BREAK_START":
            if not mapping.get("break_master_id"):
                raise HTTPException(409,"No CRM break type is mapped for this person")
            result=crm_client.start_break(
                authenticated_user_id,mapping["break_master_id"],auth_token=face_token,
            )
        else:
            result=crm_client.end_break(authenticated_user_id,auth_token=face_token)

        if not _crm_mutation_succeeded(result):
            logger.warning(
                "CRM_MANUAL_ATTENDANCE_REJECTED action=%s person_id=%s crm_user_id=%s camera_id=%s message=%s",
                action,person_id,authenticated_user_id,request.camera_id,
                str(result.get("message") or "")[:240] if isinstance(result,dict) else "",
            )
            raise HTTPException(409,"CRM rejected the attendance action")
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        status=exc.response.status_code if exc.response is not None else 502
        raise HTTPException(502,f"CRM attendance action failed (upstream HTTP {status})") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502,"CRM attendance action failed") from exc
    except Exception as exc:
        logger.exception("CRM_MANUAL_ATTENDANCE_FAILED action=%s person_id=%s camera_id=%s",action,person_id,request.camera_id)
        raise HTTPException(502,"CRM attendance action failed") from exc

    audit_id="manual-"+secrets.token_urlsafe(12)
    canonical_type={"CHECK_IN":"ATTENDANCE_ENTRY","CHECK_OUT":"ATTENDANCE_EXIT",
        "BREAK_START":"BREAK_START","BREAK_END":"BREAK_END"}[action]
    store.record_portal_event({"event_id":audit_id,"tenant_id":tenant_id,"company_code":principal.company_code,"shop_id":principal.shop_id,
        "site_id":str(camera.get("site_id") or principal.shop_id),"edge_id":request.edge_id,"camera_id":request.camera_id,
        "event_type":canonical_type,"event_time":crm_timestamp,"payload":{"person_id":person_id,"event_type":canonical_type,
        "event_time":crm_timestamp,"metadata":{"attendance_session_id":_attendance_session_id(person_id,now),
        "recognition_event_id":request.recognition_event_id,"manual":True,"confirmed_by_user_id":principal.user_id,
        "confirmed_by_name":principal.display_name,"confirmed_by_role":principal.role,"camera_name":camera.get("name"),
        "crm_user_id":authenticated_user_id}}})
    logger.info(
        "CRM_MANUAL_ATTENDANCE_SUCCESS action=%s tenant_code=%s person_id=%s crm_user_id=%s camera_id=%s audit_event_id=%s",
        action,tenant_id,person_id,authenticated_user_id,request.camera_id,audit_id,
    )
    return {"ok":True,"audit_event_id":audit_id,"action":action,"person_id":person_id,"crm_user_id":authenticated_user_id,
        "recognition_event_id":request.recognition_event_id,"confirmed_at":crm_timestamp,
        "confirmed_by":{"user_id":principal.user_id,"display_name":principal.display_name,"role":principal.role}}


@app.get("/portal/v1/tenants/{tenant_id}/attendance/roster")
def crm_attendance_roster(tenant_id: str, year: int, month: int, user_id: str | None = None,
                          principal: PortalPrincipal = Depends(require_portal_session)):
    """Proxy the authoritative CRM monthly roster through the scoped Camera Eye session."""
    _portal_scope(tenant_id, principal)
    if year < 2000 or year > 2100:
        raise HTTPException(400,"year must be between 2000 and 2100")
    if month < 1 or month > 12:
        raise HTTPException(400,"month must be between 1 and 12")
    if not crm_client.configured:
        raise HTTPException(503,"SnapKey CRM API token is not configured")
    try:
        allowed=_crm_allowed_user_ids(tenant_id)
        requested=(user_id or "").strip()
        if requested and requested not in allowed:
            logger.warning("CRM_ROSTER_BLOCKED tenant_code=%s requested_user_id=%s reason=user_not_in_tenant",
                           tenant_id,requested)
            raise HTTPException(404,"CRM user is not part of this tenant")
        rows=crm_client.users_roster(year,month,requested or None)
        if not isinstance(rows,list):
            logger.error("CRM_ROSTER_INVALID_RESPONSE tenant_code=%s response_type=%s",
                         tenant_id,type(rows).__name__)
            raise HTTPException(502,"SnapKey CRM roster returned an invalid response")
        roster_ids=_crm_roster_user_ids(rows)
        overlap=allowed & roster_ids
        if allowed and roster_ids and not overlap:
            logger.error(
                "CRM_TENANT_SCOPE_MISMATCH tenant_code=%s directory_users=%s roster_users=%s overlap=0",
                tenant_id,len(allowed),len(roster_ids),
            )
            raise HTTPException(502,"SnapKey CRM service token is scoped to a different tenant")
        scoped=[row for row in rows if isinstance(row,dict) and str(row.get("userId") or "").strip() in allowed]
        logger.info(
            "CRM_ROSTER_SCOPED tenant_code=%s requested_user_id=%s upstream_rows=%s returned_rows=%s",
            tenant_id,requested or "-",len(rows),len(scoped),
        )
        return scoped
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.exception("CRM_ROSTER_HTTP_FAILED tenant_code=%s year=%s month=%s",tenant_id,year,month)
        raise HTTPException(502,"SnapKey CRM roster lookup failed") from exc
    except RuntimeError as exc:
        logger.exception("CRM_ROSTER_SCOPE_FAILED tenant_code=%s year=%s month=%s",tenant_id,year,month)
        raise HTTPException(502,str(exc)) from exc


@app.get("/portal/v1/tenants/{tenant_id}/crm/status")
def crm_status(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    return {"configured":crm_client.configured,"base_url":crm_client.base_url,
            "mapping_count":len(store.list_crm_person_mappings(tenant_id,principal.shop_id))}

@app.get("/portal/v1/tenants/{tenant_id}/crm/users")
def crm_users(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    if not crm_client.configured: raise HTTPException(503,"SnapKey CRM API token is not configured")
    try:
        raw=crm_client.all_users()
        users=raw if isinstance(raw,list) else (raw.get("items") or raw.get("data") or [])
        # CRM AllUser identifies the customer with tenantCode (for example ABM-46-775).
        # Camera Eye tenant_id carries that same external tenant code; company_code may be an internal CRM company identifier.
        tenant_code=(tenant_id or "").strip().lower()
        items=[]
        for user in users:
            if not isinstance(user,dict) or user.get("isActive") is False: continue
            if tenant_code and str(user.get("tenantCode") or "").strip().lower()!=tenant_code: continue
            items.append({
                "id":str(user.get("id") or ""),
                "name":user.get("name") or user.get("userName") or "CRM User",
                "user_name":user.get("userName"),
                "employee_code":user.get("employeeCode"),
                "role_name":user.get("roleName"),
                "department_name":user.get("departmentName"),
                "tenant_code":user.get("tenantCode"),
                "is_admin":bool(user.get("isAdmin")),
            })
        return {"items":[x for x in items if x["id"]]}
    except httpx.HTTPError as exc: raise HTTPException(502,f"SnapKey CRM user lookup failed: {exc}") from exc

@app.get("/portal/v1/tenants/{tenant_id}/crm/breaks")
def crm_breaks(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    if not crm_client.configured: raise HTTPException(503,"SnapKey CRM API token is not configured")
    try:
        _assert_crm_service_token_scope(tenant_id)
        return {"items":crm_client.my_breaks()}
    except httpx.HTTPError as exc:
        logger.exception("CRM_BREAK_LOOKUP_HTTP_FAILED tenant_code=%s",tenant_id)
        raise HTTPException(502,"SnapKey CRM break lookup failed") from exc
    except RuntimeError as exc:
        logger.exception("CRM_BREAK_LOOKUP_SCOPE_FAILED tenant_code=%s",tenant_id)
        raise HTTPException(502,str(exc)) from exc

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


def _process_automatic_checkout(presence: dict[str, Any], *, now: datetime, reason_code: str,
                                require_camera_health: bool) -> None:
    tenant_id=str(presence["tenant_id"]); shop_id=str(presence["shop_id"])
    person_id=str(presence["local_person_id"]); crm_user_id=str(presence["crm_user_id"])
    try:
        attendance_camera_id=presence.get("last_camera_id")
        if require_camera_health and (not attendance_camera_id or not store.attendance_camera_coverage_healthy(
                tenant_id,shop_id,now,camera_id=attendance_camera_id)):
            logger.warning("AUTO_CHECKOUT_DEFERRED tenant_id=%s shop_id=%s person_id=%s reason=camera_coverage_unhealthy",
                           tenant_id,shop_id,person_id)
            store.complete_presence_checkout(tenant_id,shop_id,person_id,False)
            return
        _assert_crm_service_token_scope(tenant_id)
        result=crm_client.login_logout({
            "userId":crm_user_id,
            "date":now.date().isoformat(),
            "actualOffTime":now.strftime("%H:%M:%S"),
        })
        if not _crm_mutation_succeeded(result):
            raise RuntimeError("CRM rejected automatic checkout")
        store.complete_presence_checkout(tenant_id,shop_id,person_id,True)
        last_seen=presence["last_seen_at"]
        store.record_attendance_activity({
            "id":"activity-"+secrets.token_urlsafe(12),"tenant_id":tenant_id,"shop_id":shop_id,
            "crm_user_id":crm_user_id,"local_person_id":person_id,
            "activity_type":"CHECK_OUT","occurred_at":now,"source":"CAMERA_EYE",
            "camera_id":presence.get("last_camera_id"),"reason_code":reason_code,
            "metadata":{"last_seen_at":last_seen.isoformat() if hasattr(last_seen,"isoformat") else str(last_seen),
                        "last_recognition_event_id":presence.get("last_recognition_event_id")},
        })
        event_type="MAX_LOGOFF_AUTO_CHECK_OUT" if reason_code=="MAX_LOGOFF_REACHED" else "AUTO_CHECK_OUT"
        alert={"event_id":"auto-checkout-"+secrets.token_urlsafe(12),"tenant_id":tenant_id,"shop_id":shop_id,
               "site_id":shop_id,"edge_id":"cloud-policy","camera_id":presence.get("last_camera_id"),
               "event_type":event_type,"event_time":now.isoformat(),
               "payload":{"person_id":person_id,"metadata":{"crm_user_id":crm_user_id,
               "reason_code":reason_code,"last_recognition_event_id":presence.get("last_recognition_event_id")}}}
        store.record_portal_event(alert)
        _notify_cloud_event(alert)
        logger.info("AUTO_CHECKOUT_SUCCESS tenant_id=%s shop_id=%s person_id=%s crm_user_id=%s reason=%s",
                    tenant_id,shop_id,person_id,crm_user_id,reason_code)
    except Exception:
        store.complete_presence_checkout(tenant_id,shop_id,person_id,False)
        logger.exception("AUTO_CHECKOUT_FAILED tenant_id=%s shop_id=%s person_id=%s reason=%s",
                         tenant_id,shop_id,person_id,reason_code)




def _v2_face_token(tenant_id: str, shop_id: str, crm_user_id: str) -> str:
    """Reuse encrypted CRM token or refresh via non-attendance face login."""
    from cloud_portal.attendance_tokens import decrypt_token, encrypt_token, jwt_expiry, usable
    cached=store.get_crm_face_token(tenant_id,shop_id,crm_user_id)
    if cached and usable(cached["expires_at"]):
        return decrypt_token(cached["encrypted_token"])
    crm_tenant_id,enrolled_face=_crm_face_login_identity(tenant_id,crm_user_id)
    response=crm_client.login_using_face_tenant(enrolled_face,crm_tenant_id)
    if not _crm_face_login_succeeded(response):
        raise RuntimeError("CRM face token refresh rejected")
    crm_user=response.get("user") if isinstance(response.get("user"),dict) else {}
    if str(crm_user.get("id") or "")!=crm_user_id:
        raise RuntimeError("CRM face token refresh identity mismatch")
    token=str(response.get("token") or "").strip()
    if not token:
        raise RuntimeError("CRM face token refresh returned no token")
    expires=jwt_expiry(token)
    if not usable(expires):
        raise RuntimeError("CRM face token already expired")
    store.save_crm_face_token(tenant_id,shop_id,crm_user_id,encrypt_token(token),expires)
    return token


def _v2_auto_logout(row: dict[str, Any], now: datetime) -> None:
    """Feature gated: must not execute external attendance mutations by default."""
    if os.getenv("CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED","false").lower()!="true":
        return
    policy=row.get("policy_json") or {}
    if str(policy.get("attendanceMode") or "AUTO").upper()=="MANUAL":
        return
    tenant=str(row["tenant_id"]);shop=str(row["shop_id"]);user=str(row["crm_user_id"])
    started=row["last_seen_at"]
    # This endpoint is contractually reserved for a full 60-minute absence.
    if (now-started).total_seconds() < 60*60:
        return
    attendance_camera_id=row.get("last_camera_id")
    if not attendance_camera_id or not store.attendance_camera_coverage_healthy(
            tenant,shop,now,camera_id=attendance_camera_id):
        return
    if not store.claim_crm_auto_logout(tenant,shop,user,started):
        return
    try:
        token=_v2_face_token(tenant,shop,user)
        result=crm_client.auto_logout_with_face_token(
            user,"AUTO_LOGOUT: Employee not detected by a healthy attendance camera for 60 minutes",token)
        if not _crm_mutation_succeeded(result):
            raise RuntimeError("CRM auto-logout rejected request")
        store.complete_crm_auto_logout(tenant,shop,user,started,True)
        store.complete_presence_checkout(tenant,shop,str(row["local_person_id"]),True)
        store.record_attendance_activity({
            "id":"v2-auto-logout-"+secrets.token_urlsafe(12),
            "tenant_id":tenant,"shop_id":shop,"crm_user_id":user,
            "local_person_id":row["local_person_id"],"activity_type":"CHECK_OUT",
            "occurred_at":now,"reason_code":"ABSENCE_60_MIN_AUTO_LOGOUT",
            "source":"CAMERA_EYE","camera_id":row.get("last_camera_id"),
            "metadata":{"absence_started_at":started.isoformat(),"crm_operation":"auto-logout"},
        })
    except Exception as exc:
        # A timeout may mean CRM accepted the mutation: operator must reconcile
        # before retrying. Do not blindly resubmit a potentially successful logout.
        store.complete_crm_auto_logout(tenant,shop,user,started,False,"REQUIRES_RECONCILIATION")
        logger.exception("V2_AUTO_LOGOUT_NEEDS_RECONCILIATION user_id=%s",user)


def _evaluate_v2_person_absences() -> None:
    """Record durable person-wise absence thresholds without unsafe CRM mutations.

    CRM logout remains pending until encrypted per-user face tokens and retry
    semantics are implemented. Camera health failure must never imply absence.
    """
    if not hasattr(store,"list_v2_attendance_presence"):
        return
    from cloud_portal.person_attendance_rules import PersonAttendancePolicy, evaluate_absence
    now=datetime.now(timezone.utc)
    for row in store.list_v2_attendance_presence(limit=200):
        try:
            policy_data=row["policy_json"]
            if not policy_data.get("absenceMonitoringEnabled",True):
                continue
            policy=PersonAttendancePolicy(
                attendance_mode=policy_data.get("attendanceMode","AUTO"),
                presence_update_interval_minutes=policy_data.get("presenceUpdateIntervalMinutes",2),
                out_of_camera_grace_minutes=policy_data.get("outOfCameraGraceMinutes",5),
                max_out_of_camera_occurrences_per_day=policy_data.get("maxOutOfCameraOccurrencesPerDay",5),
                admin_notification_after_minutes=policy_data.get("adminNotificationAfterMinutes",15),
                mark_absent_after_minutes=policy_data.get("markAbsentAfterMinutes",60),
                required_working_minutes=policy_data.get("requiredWorkingMinutes",540),
                timezone=policy_data.get("timezone","Asia/Kolkata"),
            )
            tenant=str(row["tenant_id"]); shop=str(row["shop_id"])
            user=str(row["crm_user_id"]); seen=row["last_seen_at"]
            attendance_camera_id=row.get("last_camera_id")
            if not attendance_camera_id or not store.attendance_camera_coverage_healthy(
                    tenant,shop,now,camera_id=attendance_camera_id):
                continue
            from zoneinfo import ZoneInfo as _ZoneInfo
            business_day=now.astimezone(_ZoneInfo(policy.timezone)).date().isoformat()
            episodes=store.count_v2_absence_episodes(tenant,shop,user,business_day)
            # An episode is keyed by its last-seen timestamp, not by each worker tick.
            evaluation=evaluate_absence(
                policy,now=now,last_seen_at=seen,
                checked_in=bool(row["checked_in"]),on_break=bool(row["on_break"]),
                camera_coverage_healthy=True,completed_episodes_today=episodes,
                active_episode_counted=True,
            )
            transitions=list(evaluation.transitions)
            if "GRACE_EXCEEDED" in transitions and episodes >= policy.max_out_of_camera_occurrences_per_day:
                transitions.append("DAILY_ABSENCE_LIMIT_EXCEEDED")
            for transition in dict.fromkeys(transitions):
                created=store.record_v2_absence_transition(
                    tenant_id=tenant,shop_id=shop,crm_user_id=user,
                    business_date=business_day,absence_started_at=seen,
                    transition=transition,occurred_at=now,
                    details={"elapsedMinutes":round(evaluation.elapsed_minutes,2),
                             "cameraId":row.get("last_camera_id"),"state":evaluation.state},
                )
                if transition=="CRM_ABSENT_ACTION_PENDING":
                    _v2_auto_logout(row,now)
                if created and transition in ("ADMIN_ABSENCE_WARNING","DAILY_ABSENCE_LIMIT_EXCEEDED","PROLONGED_ABSENCE"):
                    try:
                        event={"event_id":f"v2-{transition}-{user}-{int(seen.timestamp())}",
                               "tenant_id":tenant,"shop_id":shop,"site_id":shop,
                               "camera_id":row.get("last_camera_id"),"edge_id":"cloud-policy",
                               "event_type":"ATTENDANCE_POLICY_VIOLATION",
                               "event_time":now.isoformat(),
                               "payload":{"metadata":{"crm_user_id":user,
                                   "reason_code":transition,
                                   "elapsed_minutes":round(evaluation.elapsed_minutes,2)}}}
                        _notify_cloud_event(event)
                    except Exception:
                        logger.exception("V2_NOTIFICATION_FAILED user_id=%s transition=%s",user,transition)
                if created:
                    logger.info("V2_ATTENDANCE_TRANSITION tenant_id=%s shop_id=%s crm_user_id=%s transition=%s",
                                tenant,shop,user,transition)
        except Exception:
            logger.exception("V2_ATTENDANCE_EVALUATION_FAILED user_id=%s",row.get("crm_user_id"))


def _evaluate_absence_checkouts() -> None:
    """Evaluate absence and maximum-logoff rules after heartbeats."""
    if not hasattr(store,"claim_due_absence_checkouts"):
        return
    _evaluate_v2_person_absences()
    now=datetime.now(timezone.utc)
    for presence in store.claim_due_max_logoff_checkouts(now,limit=50):
        _process_automatic_checkout(presence,now=now,reason_code="MAX_LOGOFF_REACHED",
                                    require_camera_health=False)
    for presence in store.claim_due_absence_checkouts(now,limit=50):
        _process_automatic_checkout(presence,now=now,reason_code="ABSENCE_GRACE_EXCEEDED",
                                    require_camera_health=True)


def _update_root() -> Path:
    root=Path(os.getenv("SNAPKEY_UPDATE_ROOT","/app/data/updates")).resolve()
    root.mkdir(parents=True,exist_ok=True)
    return root


def _update_manifest() -> dict[str, Any] | None:
    path=_update_root()/"latest.json"
    if not path.is_file(): return None
    try: return json.loads(path.read_text(encoding="utf-8"))
    except (OSError,ValueError): return None


@app.get("/edge/v1/updates/latest")
def edge_latest_update(current_build_id: str = "", principal: EdgePrincipal = Depends(require_edge_token)):
    manifest=_update_manifest()
    if not manifest:
        return {"available":False,"current_build_id":current_build_id}
    return {**manifest,"available":str(manifest.get("build_id") or "") != str(current_build_id or "")}


@app.get("/edge/v1/updates/{build_id}/download")
def edge_download_update(build_id: str, principal: EdgePrincipal = Depends(require_edge_token)):
    manifest=_update_manifest()
    if not manifest or not hmac.compare_digest(str(manifest.get("build_id") or ""),str(build_id)):
        raise HTTPException(404,"Camera Eye update not found")
    path=(_update_root()/str(manifest["filename"])).resolve()
    if not path.is_relative_to(_update_root()) or not path.is_file():
        raise HTTPException(404,"Camera Eye update file not found")
    return FileResponse(path,media_type="application/vnd.microsoft.portable-executable",filename="MadhushalaCameraAISetup.exe",
                        headers={"Cache-Control":"private, no-store"})


@app.post("/internal/v1/edge-updates/publish")
async def publish_edge_update(request: Request, file: UploadFile = File(...),
                              x_update_publish_key: str | None = Header(default=None)):
    expected=os.getenv("SNAPKEY_UPDATE_PUBLISH_KEY","").strip()
    if not expected or not x_update_publish_key or not hmac.compare_digest(expected,x_update_publish_key):
        raise HTTPException(401,"Invalid update publishing credential")
    version=(request.query_params.get("version") or "").strip()
    build_id=(request.query_params.get("build_id") or "").strip()
    supplied_sha=(request.query_params.get("sha256") or "").strip().lower()
    if not version or not build_id or len(supplied_sha)!=64 or not all(ch in "0123456789abcdef" for ch in supplied_sha):
        raise HTTPException(400,"version, build_id and valid sha256 are required")
    safe_build="".join(ch for ch in build_id if ch.isalnum() or ch in "-_.")
    if safe_build != build_id or not safe_build:
        raise HTTPException(400,"Invalid build_id")
    root=_update_root(); filename=f"MadhushalaCameraAISetup-{safe_build}.exe"
    target=(root/filename).resolve(); temporary=target.with_suffix(".exe.part")
    digest=hashlib.sha256(); size=0
    try:
        with temporary.open("wb") as output:
            while True:
                chunk=await file.read(1024*1024)
                if not chunk: break
                size += len(chunk)
                if size > 1024*1024*1024: raise HTTPException(413,"Update installer is too large")
                digest.update(chunk); output.write(chunk)
        actual=digest.hexdigest()
        if not hmac.compare_digest(actual,supplied_sha):
            raise HTTPException(400,"Installer SHA-256 does not match publisher metadata")
        temporary.replace(target)
        manifest={"version":version,"build_id":build_id,"sha256":actual,"filename":filename,
                  "size_bytes":size,"published_at":datetime.now(timezone.utc).isoformat()}
        manifest_tmp=root/"latest.json.part"; manifest_tmp.write_text(json.dumps(manifest),encoding="utf-8")
        manifest_tmp.replace(root/"latest.json")
        return {"ok":True,**manifest}
    finally:
        temporary.unlink(missing_ok=True)



def _model_root() -> Path:
    root = Path(os.getenv("SNAPKEY_MODEL_ROOT", "/app/data/models")).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _model_catalog() -> dict[str, Any]:
    path = _model_root() / "manifest.json"
    if not path.is_file():
        return {"models": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"models": []}
    except (OSError, ValueError):
        return {"models": []}


def _model_visible(item: dict[str, Any], principal: EdgePrincipal) -> bool:
    # The scoped edge credential is the authorization boundary. Feature filtering
    # can be tightened to the signed license once license claims are persisted server-side.
    return bool(item.get("required", False) or item.get("feature"))


@app.get("/edge/v1/models/manifest")
def edge_model_manifest(principal: EdgePrincipal = Depends(require_edge_token)):
    items = []
    for item in _model_catalog().get("models") or []:
        if _model_visible(item, principal):
            public = {key: item.get(key) for key in ("id", "version", "runtime", "feature", "required", "sha256", "size_bytes", "filename")}
            items.append(public)
    return {"schema_version": "camera-eye.models.v1", "models": items}


@app.get("/edge/v1/models/{model_id}/{version}/download")
def edge_download_model(model_id: str, version: str, principal: EdgePrincipal = Depends(require_edge_token)):
    catalog = _model_catalog()
    item = next((entry for entry in catalog.get("models") or []
                 if str(entry.get("id")) == model_id and str(entry.get("version")) == version and _model_visible(entry, principal)), None)
    if not item:
        raise HTTPException(404, "Model artifact not found")
    path = (_model_root() / str(item.get("storage_path") or "")).resolve()
    if not path.is_relative_to(_model_root()) or not path.is_file():
        raise HTTPException(404, "Model artifact file not found")
    return FileResponse(path, media_type="application/octet-stream", filename=str(item.get("filename") or path.name),
                        headers={"Cache-Control": "private, no-store"})


@app.post("/internal/v1/models/publish")
async def publish_model(request: Request, file: UploadFile = File(...),
                        x_model_publish_key: str | None = Header(default=None)):
    expected = os.getenv("SNAPKEY_MODEL_PUBLISH_KEY", "").strip()
    if not expected or not x_model_publish_key or not hmac.compare_digest(expected, x_model_publish_key):
        raise HTTPException(401, "Invalid model publishing credential")
    qp = request.query_params
    model_id = (qp.get("model_id") or "").strip().lower()
    version = (qp.get("version") or "").strip()
    runtime = (qp.get("runtime") or "").strip().upper()
    feature = (qp.get("feature") or "").strip().lower() or None
    required = (qp.get("required") or "false").strip().lower() in {"1", "true", "yes"}
    supplied_sha = (qp.get("sha256") or "").strip().lower()
    safe = lambda value: value and all(ch.isalnum() or ch in "-_." for ch in value)
    if not safe(model_id) or not safe(version) or runtime not in {"PYTORCH", "ONNX", "OPENVINO", "INSIGHTFACE"}:
        raise HTTPException(400, "Valid model_id, version and runtime are required")
    if len(supplied_sha) != 64 or not all(ch in "0123456789abcdef" for ch in supplied_sha):
        raise HTTPException(400, "Valid sha256 is required")
    filename = Path(file.filename or "model.bin").name
    rel = Path(model_id) / version / filename
    target = (_model_root() / rel).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".part")
    digest = hashlib.sha256(); size = 0
    try:
        with temporary.open("wb") as output:
            while True:
                chunk = await file.read(1024 * 1024)
                if not chunk: break
                size += len(chunk)
                if size > 2 * 1024 * 1024 * 1024:
                    raise HTTPException(413, "Model artifact is too large")
                digest.update(chunk); output.write(chunk)
        actual = digest.hexdigest()
        if not hmac.compare_digest(actual, supplied_sha):
            raise HTTPException(400, "Model SHA-256 does not match publisher metadata")
        temporary.replace(target)
        catalog = _model_catalog()
        models = [entry for entry in (catalog.get("models") or []) if str(entry.get("id")) != model_id]
        models.append({"id": model_id, "version": version, "runtime": runtime, "feature": feature,
                       "required": required, "sha256": actual, "size_bytes": size, "filename": filename,
                       "storage_path": rel.as_posix(), "published_at": datetime.now(timezone.utc).isoformat()})
        catalog = {"schema_version": "camera-eye.models.v1", "models": sorted(models, key=lambda x: x["id"])}
        tmp = _model_root() / "manifest.json.part"
        tmp.write_text(json.dumps(catalog, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(_model_root() / "manifest.json")
        return {"ok": True, **models[-1]}
    finally:
        temporary.unlink(missing_ok=True)


@app.post("/edge/v1/heartbeat")
def edge_heartbeat(payload: dict[str, Any], background_tasks: BackgroundTasks,
                   principal: EdgePrincipal = Depends(require_edge_token)):
    required = ["tenant_id", "site_id", "edge_id", "status"]
    missing = [key for key in required if payload.get(key) is None]
    if missing:
        raise HTTPException(400, {"missing": missing})
    _enforce_edge_scope(principal, payload)
    result=store.record_heartbeat(payload)
    background_tasks.add_task(_evaluate_absence_checkouts)
    return result
