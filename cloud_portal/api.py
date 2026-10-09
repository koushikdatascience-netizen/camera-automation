from __future__ import annotations

import base64
from contextlib import nullcontext
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
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from pydantic import BaseModel, Field

from camera_service.licensing import sign_license_payload
from cloud_portal.storage import PortalStore
from cloud_portal.crm_client import crm_client
from cloud_portal.attendance_policy import AttendancePolicy
from cloud_portal.notifications import NotificationService
from camera_service.face_service import FaceService, enrollment_model_key, validated_embedding
from camera_service.face_provenance import template_diagnostics
from cloud_portal.enrollment_worker import EnrollmentWorker

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
CRM_AUTO_LOGOUT_MIN_ABSENCE_MINUTES = 60  # fixed by /api/UserActivity/auto-logout contract

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
_cloud_face_init_lock = threading.Lock()
_crm_enrollment_worker = EnrollmentWorker(
    concurrency=max(1,min(4,int(os.getenv("SNAPKEY_ENROLLMENT_WORKERS","1")))),
    capacity=max(1,int(os.getenv("SNAPKEY_ENROLLMENT_QUEUE_SIZE","32"))))
_crm_enrollment_metrics = {"cache_hits":0,"inference_count":0,"model_initializations":0,"rejected_images":0,"rejected_cache_hits":0}

@app.on_event("startup")
def start_enrollment_worker():
    global _crm_enrollment_worker
    if _crm_enrollment_worker.closed:
        _crm_enrollment_worker=EnrollmentWorker(
            concurrency=max(1,min(4,int(os.getenv("SNAPKEY_ENROLLMENT_WORKERS","1")))),
            capacity=max(1,int(os.getenv("SNAPKEY_ENROLLMENT_QUEUE_SIZE","32"))))

@app.on_event("shutdown")
def shutdown_enrollment_worker():
    _crm_enrollment_worker.shutdown()

def _cloud_face_enroller() -> FaceService:
    global _cloud_face_service
    with _cloud_face_init_lock:
        if _cloud_face_service is None:
            _cloud_face_service=FaceService(None)
            _crm_enrollment_metrics["model_initializations"]+=1
    return _cloud_face_service

def _crm_personnel_refresh_lock(tenant_id: str, shop_id: str) -> threading.Lock:
    key=(tenant_id,shop_id)
    with _crm_personnel_refresh_guard:
        return _crm_personnel_refresh_locks.setdefault(key,threading.Lock())

def _crm_personnel_refresh_due(tenant_id: str, shop_id: str) -> bool:
    ttl=max(15,int(os.getenv("SNAPKEY_CRM_PERSONNEL_REFRESH_SECONDS","60")))
    last=_crm_personnel_last_refresh.get((tenant_id,shop_id))
    return last is None or monotonic_time.monotonic()-last >= ttl

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
    request_id:str|None=Field(default=None,max_length=128)


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

class AttendanceLogoutReconciliationRequest(BaseModel):
    outcome: str = Field(pattern="^(CRM_CONFIRMED|CRM_NOT_APPLIED)$")

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
    # The integration key authenticates the trusted CRM backend, which supplies
    # tenant/shop identity per request. Never provision tenant IDs through env.
    # This is a service-to-service trust boundary: never expose the key to a browser.
    if not tenant_id.strip() or not shop_id.strip():
        raise HTTPException(400, "tenant_id and shop_id are required")
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
    saved=store.upsert_person_attendance_policy(tenant_id, shop_id, crm_user_id, payload.model_dump(exclude_unset=True))
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
    coverage=(store.get_v2_camera_coverage(tenant_id,shop_id,crm_user_id)
              if hasattr(store,"get_v2_camera_coverage") else None)
    return {"presence":presence,"cameraCoverageHealth":coverage or {
                "state":"UNKNOWN","reason":"not_evaluated","healthyCameraIds":[]},
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
    metadata=activity.get("metadata") or {}
    recognition_event_id=str(evidence.get("recognitionEventId") or metadata.get("recognition_event_id") or "")
    prefix=(f"/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}"
            f"/activities/{activity_id}/evidence")
    snapshots=evidence.get("snapshots") if isinstance(evidence.get("snapshots"),list) else []
    urls=[]
    if recognition_event_id:
        urls=[f"{prefix}/snapshots/{int(item.get('index',position))}"
              for position,item in enumerate(snapshots[:3])
              if isinstance(item,dict) and isinstance(item.get("evidence"),dict)
              and item["evidence"].get("evidence_id")]
    video_url=f"{prefix}/video" if recognition_event_id and evidence.get("video") else None
    return {"activityId":activity_id,"status":evidence.get("status","UNAVAILABLE"),
            "evidenceAvailable":bool(snapshots or evidence.get("video")),"manifest":evidence,
            "snapshotUrls":urls,"videoUrl":video_url,
            "mediaAccess":"SCOPED_AUTHENTICATED_PROXY" if recognition_event_id else "UNAVAILABLE"}


def _integration_attendance_media(tenant_id: str,shop_id: str,crm_user_id: str,
                                  activity_id: str,index: int | None=None,kind: str="snapshot") -> Path:
    activity=store.get_person_attendance_activity(tenant_id,shop_id,crm_user_id,activity_id)
    if not activity:
        raise HTTPException(404,"Attendance activity not found")
    evidence=activity.get("evidence") or {}
    metadata=activity.get("metadata") or {}
    event_id=str(evidence.get("recognitionEventId") or metadata.get("recognition_event_id") or "")
    if not event_id:
        raise HTTPException(404,"Action-linked evidence not available")
    return _resolve_attendance_event_media(tenant_id,shop_id,event_id,kind,index or 0)


@app.get("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/activities/{activity_id}/evidence/snapshots/{index}")
def integration_person_attendance_evidence_snapshot(tenant_id: str,shop_id: str,crm_user_id: str,
        activity_id: str,index: int,request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    if index<0 or index>2:
        raise HTTPException(404,"Snapshot not available")
    path=_integration_attendance_media(tenant_id,shop_id,crm_user_id,activity_id,index,"snapshot")
    return FileResponse(path,headers={"Cache-Control":"private, no-store"})


@app.get("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/activities/{activity_id}/evidence/video")
def integration_person_attendance_evidence_video(tenant_id: str,shop_id: str,crm_user_id: str,
        activity_id: str,request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    path=_integration_attendance_media(tenant_id,shop_id,crm_user_id,activity_id,kind="video")
    return FileResponse(path,media_type="video/webm" if path.suffix.lower()==".webm" else "video/mp4",headers={"Cache-Control":"private, no-store"})


@app.get("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/auto-logout-actions")
def integration_person_auto_logout_actions(tenant_id: str,shop_id: str,crm_user_id: str,
                                           request: Request,limit: int=100):
    _require_crm_integration(request,tenant_id,shop_id)
    if not 1<=limit<=200:
        raise HTTPException(422,"Invalid limit")
    if not hasattr(store,"list_v2_crm_auto_logout_actions"):
        raise HTTPException(503,"Auto-logout reconciliation requires PostgreSQL")
    rows=store.list_v2_crm_auto_logout_actions(tenant_id,shop_id,crm_user_id,limit)
    for row in rows:
        row["actionId"]=_v2_auto_logout_action_id(tenant_id,shop_id,crm_user_id,row["absence_started_at"])
    return {"items":rows,"note":"CRM-reconciliation-required actions must be checked against CRM before resolution"}


@app.post("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/auto-logout-actions/{action_id}/reconcile")
def integration_reconcile_person_auto_logout(tenant_id: str,shop_id: str,crm_user_id: str,
        action_id: str,payload: AttendanceLogoutReconciliationRequest,request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    if not hasattr(store,"list_v2_crm_auto_logout_actions"):
        raise HTTPException(503,"Auto-logout reconciliation requires PostgreSQL")
    rows=store.list_v2_crm_auto_logout_actions(tenant_id,shop_id,crm_user_id,200)
    action=next((item for item in rows if _v2_auto_logout_action_id(
        tenant_id,shop_id,crm_user_id,item["absence_started_at"])==action_id),None)
    if not action or action.get("status")!="RECONCILIATION_REQUIRED":
        raise HTTPException(404,"Reconciliation-required CRM action not found")
    started=action["absence_started_at"]
    if not store.reconcile_crm_auto_logout_action(tenant_id,shop_id,crm_user_id,started,payload.outcome):
        raise HTTPException(409,"CRM action changed; reload reconciliation status")
    if payload.outcome=="CRM_NOT_APPLIED":
        return {"actionId":action_id,"status":"PENDING",
                "note":"A safe retry is queued and remains behind the feature flag"}
    activity_id="v2-"+action_id
    state=store.finalize_crm_auto_logout_local(tenant_id,shop_id,crm_user_id,started,{
        "id":activity_id,"occurred_at":datetime.now(timezone.utc),"camera_id":action.get("camera_id"),
        "evidence":_evidence_manifest_for_last_recognition(tenant_id,shop_id,
            action.get("last_recognition_event_id"),"reconciled_absence_action_snapshot"),
        "metadata":{"absence_started_at":started.isoformat(),"crm_operation":"auto-logout",
                    "recognition_event_id":action.get("last_recognition_event_id"),
                    "reconciled_by":"CRM_INTEGRATION"},
    })
    return {"actionId":action_id,"status":state}



@app.get("/integration/v2/tenants/{tenant_id}/shops/{shop_id}/attendance/users/{crm_user_id}/alerts")
def integration_person_attendance_alerts(tenant_id: str, shop_id: str, crm_user_id: str,
                                         request: Request, day: date, limit: int = 100):
    _require_crm_integration(request,tenant_id,shop_id)
    if not 1 <= limit <= 200:
        raise HTTPException(422,"Invalid limit")
    if not hasattr(store,"list_v2_absence_alerts"):
        raise HTTPException(503,"V2 alerts require PostgreSQL")
    items=store.list_v2_absence_alerts(tenant_id,shop_id,crm_user_id,day.isoformat(),limit)
    if hasattr(store,"notification_delivery_status"):
        for item in items:
            transition=str(item.get("transition") or "")
            started=item.get("absence_started_at")
            if transition in {"ADMIN_ABSENCE_WARNING","DAILY_ABSENCE_LIMIT_EXCEEDED","PROLONGED_ABSENCE"} and started:
                event_id=f"v2-{transition}-{crm_user_id}-{int(started.timestamp())}"
                item["notificationDelivery"]=store.notification_delivery_status(tenant_id,shop_id,event_id)
    return {"items":items,"date":day.isoformat(),"notificationDelivery":"DURABLE_OUTBOX"}

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
    if not hasattr(store,"enqueue_notification_delivery"):
        logger.error("NOTIFICATION_OUTBOX_UNAVAILABLE event_type=%s",event_type)
        return
    person_policy={}
    crm_user_id=str(metadata.get("crm_user_id") or "")
    if crm_user_id and hasattr(store,"person_attendance_policy"):
        person_policy=store.person_attendance_policy(tenant_id,shop_id,crm_user_id) or {}
    event_id=str(envelope.get("event_id") or hashlib.sha256(
        json.dumps(envelope,sort_keys=True,default=str).encode()).hexdigest())
    if person_policy.get("emailNotificationsEnabled",True):
        for recipient in person_policy.get("email_recipients",policy.get("email_recipients")) or []:
            store.enqueue_notification_delivery(tenant_id,shop_id,event_id,"email",str(recipient),
                {"subject":f"Camera Eye - {event_type}","body":body})
    if person_policy.get("whatsappNotificationsEnabled",True):
        for recipient in person_policy.get("whatsapp_recipients",policy.get("whatsapp_recipients")) or []:
            store.enqueue_notification_delivery(tenant_id,shop_id,event_id,"whatsapp",str(recipient),
                {"body":body})
    logger.info("NOTIFICATION_ENQUEUED event_id=%s event_type=%s",event_id,event_type)


def _dispatch_notification_outbox(limit: int = 50) -> None:
    if not hasattr(store,"claim_due_notification_deliveries"):
        return
    now=datetime.now(timezone.utc)
    for item in store.claim_due_notification_deliveries(now,limit=limit):
        delivery_id=str(item["id"]);attempts=int(item.get("attempts") or 1)
        channel=str(item.get("channel") or "");recipient=str(item.get("recipient") or "")
        payload=item.get("payload") or {}
        try:
            if channel=="email":
                result=notification_service.send_email([recipient],str(payload.get("subject") or "Camera Eye alert"),
                                                       str(payload.get("body") or ""))
                delivered=bool(result.delivered);detail=result.detail
            elif channel=="whatsapp":
                results=notification_service.send_whatsapp_text([recipient],str(payload.get("body") or ""))
                delivered=bool(results and results[0].delivered)
                detail=results[0].detail if results else "no_result"
            else:
                delivered=False;detail="unsupported_channel"
            retry_after=min(3600,2**min(attempts,10))
            store.complete_notification_delivery(delivery_id,success=delivered,error="" if delivered else detail,
                retry_after_seconds=retry_after,attempts=attempts,
                max_attempts=int(os.getenv("SNAPKEY_NOTIFICATION_MAX_ATTEMPTS","8")))
            logger.info("NOTIFICATION_DELIVERY id=%s channel=%s delivered=%s attempt=%s",
                        delivery_id,channel,delivered,attempts)
        except Exception as exc:
            retry_after=min(3600,2**min(attempts,10))
            try:
                store.complete_notification_delivery(delivery_id,success=False,error=type(exc).__name__,
                    retry_after_seconds=retry_after,attempts=attempts,
                    max_attempts=int(os.getenv("SNAPKEY_NOTIFICATION_MAX_ATTEMPTS","8")))
            except Exception:
                logger.exception("NOTIFICATION_OUTBOX_COMPLETE_FAILED id=%s",delivery_id)
            logger.warning("NOTIFICATION_DELIVERY_FAILED id=%s channel=%s error_type=%s",
                           delivery_id,channel,type(exc).__name__)


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
    allowed = {"index.html", "system-status.html", "cameras.html", "personnel.html", "attendance.html", "live.html", "alerts.html", "verification.html"}
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

class DetectionZonePayload(BaseModel):
    id: str = Field(min_length=1,max_length=128)
    name: str = Field(default="Detection Zone",min_length=1,max_length=80)
    x: float = Field(ge=0,le=1)
    y: float = Field(ge=0,le=1)
    width: float = Field(gt=0,le=1)
    height: float = Field(gt=0,le=1)
    enabled: bool = True

class DetectionConfigPayload(BaseModel):
    mode: str
    zones: list[DetectionZonePayload] = Field(default_factory=list,max_length=64)
    expected_version: int = Field(ge=0)

class DetectionZoneMutation(DetectionZonePayload):
    expected_version: int = Field(ge=0)

class DetectionConfigAck(BaseModel):
    version: int = Field(ge=0)
    status: str
    local_override: bool = False



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
                keyed[key].update({k: advertised.get(k) for k in ("online","state","last_frame_at","capture_fps","ai_fps","last_error","frame_width","frame_height")})
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
        if existing and str(existing.get("source") or "").strip():
            payload["source"] = existing["source"]
        elif _edge_inventory_camera(tenant_id, principal.shop_id, request.edge_id, camera_id):
            # The edge advertises local cameras without exposing their device source.
            # Keep the source on the edge; cloud assignments resolve this reference locally.
            payload["source"] = f"edge-local:{camera_id}"
        else:
            raise HTTPException(400, "Existing camera source could not be preserved")
    elif not request.source.strip():
        raise HTTPException(400, "Camera source is required")
    return {"camera": _portal_camera_view(store.upsert_camera(payload))}


def _camera_detection_scope(tenant_id: str, shop_id: str, edge_id: str, camera_id: str) -> dict[str, Any]:
    camera=store.get_camera(tenant_id,shop_id,edge_id,camera_id)
    if not camera:
        raise HTTPException(404,"Camera not found in this tenant, shop, and edge")
    return camera


def _effective_detection_config(tenant_id: str,shop_id: str,edge_id: str,camera_id: str)->dict[str,Any]:
    config=store.get_detection_config(tenant_id,shop_id,edge_id,camera_id)
    camera=store.get_camera(tenant_id,shop_id,edge_id,camera_id) or {}
    features=camera.get("features") or {}
    enabled=bool(features.get("unknown_detection") or features.get("unknown_person_detection"))
    if not enabled: config={**config,"effective_mode":"DISABLED","effective_zones":[]}
    return config


@app.get("/portal/v1/tenants/{tenant_id}/cameras/{camera_id}/detection-config")
def portal_get_detection_config(tenant_id: str, camera_id: str, shop_id: str, edge_id: str,
                                principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    if shop_id != principal.shop_id: raise HTTPException(403,"Portal session is not authorized for this shop")
    _camera_detection_scope(tenant_id,shop_id,edge_id,camera_id)
    return _effective_detection_config(tenant_id,shop_id,edge_id,camera_id)


@app.put("/portal/v1/tenants/{tenant_id}/cameras/{camera_id}/detection-config")
def portal_put_detection_config(tenant_id: str, camera_id: str, shop_id: str, edge_id: str,
                                payload: DetectionConfigPayload,
                                principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal); _portal_scope(tenant_id,principal)
    if shop_id != principal.shop_id: raise HTTPException(403,"Portal session is not authorized for this shop")
    _camera_detection_scope(tenant_id,shop_id,edge_id,camera_id)
    try:
        store.replace_detection_config(tenant_id,shop_id,edge_id,camera_id,payload.mode,
            [z.model_dump() for z in payload.zones],payload.expected_version)
        return _effective_detection_config(tenant_id,shop_id,edge_id,camera_id)
    except RuntimeError as exc: raise HTTPException(409,str(exc))
    except ValueError as exc: raise HTTPException(422,str(exc))


@app.get("/integration/v1/tenants/{tenant_id}/shops/{shop_id}/cameras/{camera_id}/detection-config")
def crm_get_detection_config(tenant_id: str, shop_id: str, camera_id: str, edge_id: str, request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    _camera_detection_scope(tenant_id,shop_id,edge_id,camera_id)
    return _effective_detection_config(tenant_id,shop_id,edge_id,camera_id)


@app.put("/integration/v1/tenants/{tenant_id}/shops/{shop_id}/cameras/{camera_id}/detection-config")
def crm_put_detection_config(tenant_id: str, shop_id: str, camera_id: str, edge_id: str,
                             payload: DetectionConfigPayload, request: Request):
    _require_crm_integration(request,tenant_id,shop_id)
    _camera_detection_scope(tenant_id,shop_id,edge_id,camera_id)
    try:
        store.replace_detection_config(tenant_id,shop_id,edge_id,camera_id,payload.mode,
            [z.model_dump() for z in payload.zones],payload.expected_version)
        saved=_effective_detection_config(tenant_id,shop_id,edge_id,camera_id)
    except RuntimeError as exc: raise HTTPException(409,str(exc))
    except ValueError as exc: raise HTTPException(422,str(exc))
    logger.info("CAMERA_DETECTION_CONFIG_UPDATED tenant_id=%s shop_id=%s edge_id=%s camera_id=%s version=%s mode=%s",
        tenant_id,shop_id,edge_id,camera_id,saved["version"],saved["mode"])
    return saved


def _mutate_zone(tenant_id: str,shop_id: str,edge_id: str,camera_id: str,zone_id: str,
                 zone: dict[str,Any] | None,expected_version: int,delete: bool=False):
    current=store.get_detection_config(tenant_id,shop_id,edge_id,camera_id)
    if int(current["version"])!=expected_version: raise HTTPException(409,"Detection configuration version conflict")
    zones=list(current["zones"])
    index=next((i for i,item in enumerate(zones) if str(item["id"])==zone_id),None)
    if delete:
        if index is None: raise HTTPException(404,"Detection zone not found")
        zones.pop(index)
    elif index is None: zones.append(zone)
    else: zones[index]=zone
    try:
        store.replace_detection_config(tenant_id,shop_id,edge_id,camera_id,"CUSTOM_ZONES",zones,expected_version)
        return _effective_detection_config(tenant_id,shop_id,edge_id,camera_id)
    except RuntimeError as exc: raise HTTPException(409,str(exc))
    except ValueError as exc: raise HTTPException(422,str(exc))


@app.post("/integration/v1/tenants/{tenant_id}/shops/{shop_id}/cameras/{camera_id}/detection-zones")
def crm_create_detection_zone(tenant_id: str,shop_id: str,camera_id: str,edge_id: str,payload: DetectionZoneMutation,request: Request):
    _require_crm_integration(request,tenant_id,shop_id); _camera_detection_scope(tenant_id,shop_id,edge_id,camera_id)
    try: return _mutate_zone(tenant_id,shop_id,edge_id,camera_id,payload.id,payload.model_dump(exclude={"expected_version"}),payload.expected_version)
    except HTTPException: raise


@app.put("/integration/v1/tenants/{tenant_id}/shops/{shop_id}/cameras/{camera_id}/detection-zones/{zone_id}")
def crm_update_detection_zone(tenant_id: str,shop_id: str,camera_id: str,zone_id: str,edge_id: str,payload: DetectionZoneMutation,request: Request):
    _require_crm_integration(request,tenant_id,shop_id); _camera_detection_scope(tenant_id,shop_id,edge_id,camera_id)
    if payload.id!=zone_id: raise HTTPException(400,"Zone ID does not match request path")
    return _mutate_zone(tenant_id,shop_id,edge_id,camera_id,zone_id,payload.model_dump(exclude={"expected_version"}),payload.expected_version)


@app.delete("/integration/v1/tenants/{tenant_id}/shops/{shop_id}/cameras/{camera_id}/detection-zones/{zone_id}")
def crm_delete_detection_zone(tenant_id: str,shop_id: str,camera_id: str,zone_id: str,edge_id: str,expected_version: int,request: Request):
    _require_crm_integration(request,tenant_id,shop_id); _camera_detection_scope(tenant_id,shop_id,edge_id,camera_id)
    return _mutate_zone(tenant_id,shop_id,edge_id,camera_id,zone_id,None,expected_version,True)


@app.get("/integration/v1/tenants/{tenant_id}/shops/{shop_id}/unknown-incidents")
def crm_unknown_incidents(tenant_id: str, shop_id: str, request: Request, edge_id: str | None = None,
                         camera_id: str | None = None, limit: int = 100):
    _require_crm_integration(request,tenant_id,shop_id)
    if not 1<=limit<=500: raise HTTPException(422,"limit must be between 1 and 500")
    rows=store.list_events(tenant_id,event_type="UNKNOWN_INCIDENT",shop_id=shop_id,limit=limit)
    items=[]
    for row in rows:
        if edge_id and str(row.get("edge_id") or "")!=edge_id: continue
        if camera_id and str(row.get("camera_id") or "")!=camera_id: continue
        payload=row.get("payload") or {}
        items.append({"event_id":row.get("id"),"tenant_id":tenant_id,"shop_id":shop_id,
            "edge_id":row.get("edge_id"),"camera_id":row.get("camera_id"),"event_time":str(row.get("event_time") or ""),
            "event_type":row.get("event_type"),"incident":payload.get("payload",payload)})
    return {"items":items}


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
    items=store.list_cameras(principal.tenant_id, shop_id=principal.shop_id, edge_id=principal.edge_id)
    for camera in items:
        camera["detection_config"]=_effective_detection_config(principal.tenant_id,principal.shop_id,principal.edge_id,camera["camera_id"])
    return {
        "tenant_id": principal.tenant_id,
        "company_code": principal.company_code,
        "shop_id": principal.shop_id,
        "site_id": principal.site_id,
        "edge_id": principal.edge_id,
        "items": items,
    }


@app.post("/edge/v1/config/cameras/{camera_id}/detection-config/ack")
def edge_detection_config_ack(camera_id: str, payload: DetectionConfigAck,
                              principal: EdgePrincipal = Depends(require_edge_token)):
    if principal.legacy_global: raise HTTPException(403,"Scoped edge credential is required for configuration acknowledgements")
    if payload.status not in {"APPLIED","LOCAL_OVERRIDE","FAILED"}: raise HTTPException(422,"Invalid acknowledgement status")
    ok=store.acknowledge_detection_config(principal.tenant_id,principal.shop_id,principal.edge_id,
        camera_id,payload.version,payload.status,payload.local_override)
    if not ok: raise HTTPException(409,"Camera configuration version no longer matches")
    return {"ok":True,"camera_id":camera_id,"version":payload.version,"status":payload.status}


@app.get("/edge/v1/config/personnel")
def edge_personnel_config(principal: EdgePrincipal = Depends(require_edge_token)):
    if principal.legacy_global: raise HTTPException(403,"Scoped edge credential is required for personnel configuration")
    # Edge polling must remain cheap. CRM synchronization is TTL-cached and
    # single-flight; normal polls serve the already mirrored personnel immediately.
    _refresh_crm_personnel(str(principal.tenant_id), str(principal.shop_id))
    items=[]
    for person in store.list_cloud_people(principal.tenant_id,principal.shop_id):
        faces=store.list_cloud_faces(principal.tenant_id,principal.shop_id,str(person["id"]),include_embedding=True)
        faces=[f for f in faces if not str(f['id']).startswith('crm-image:') or f.get('model_key')]
        model_keys={f['model_key'] for f in faces if str(f['id']).startswith('crm-image:')}
        if len(model_keys)>1: raise HTTPException(503,'CRM enrollment model refresh is incomplete')
        mapping=store.crm_person_mapping(principal.tenant_id,principal.shop_id,str(person['id']))
        policy=(store.person_attendance_policy(principal.tenant_id,principal.shop_id,str(mapping['crm_user_id']))
                if mapping and hasattr(store,'person_attendance_policy') else {}) or {}
        items.append({"person_id":str(person["id"]),"employee_code":person["employee_code"],"full_name":person["full_name"],
            "attendance_mode":str(policy.get('attendanceMode') or 'AUTO').upper() if mapping else 'MANUAL',
            "crm_user_id":str(mapping['crm_user_id']) if mapping else None,
            "tenant_id":principal.tenant_id,"shop_id":principal.shop_id,
            "enrollment_model_key":next(iter(model_keys),None),
            "role":person["role"],"phone":person.get("phone"),"email":person.get("email"),"active":bool(person["active"]),
            "faces":[{"face_id":str(f["id"]),"embedding":f["embedding"],"quality":float(f["quality"]),"model_key":f.get("model_key")} for f in faces]})
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
    allowed = {"image/jpeg", "image/jpg", "image/png", "image/webp", "video/mp4", "video/webm"}
    if content_type not in allowed:
        raise HTTPException(415, "Only JPEG, PNG, WebP, MP4, and WebM evidence are supported")
    max_bytes = 50 * 1024 * 1024 if content_type.startswith("video/") else 5 * 1024 * 1024
    data = await file.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise HTTPException(413, "Evidence file exceeds the allowed size")
    suffix = {"image/png": ".png", "image/webp": ".webp", "video/mp4": ".mp4", "video/webm": ".webm"}.get(content_type, ".jpg")
    root = Path(os.getenv("SNAPKEY_EVIDENCE_ROOT", "/app/data/evidence"))
    root = root.resolve()
    target_dir = (root / str(principal.tenant_id) / str(principal.shop_id) / str(principal.edge_id)).resolve()
    if not target_dir.is_relative_to(root):
        raise HTTPException(403, 'Invalid evidence scope')
    target_dir.mkdir(parents=True, exist_ok=True)
    filename_id = hashlib.sha256(event_id.encode("utf-8")).hexdigest()
    target = target_dir / f"{filename_id}{suffix}"
    target.write_bytes(data)
    return {
        "event_id": event_id,
        "evidence_id": f"{principal.tenant_id}/{principal.shop_id}/{principal.edge_id}/{filename_id}{suffix}",
        "content_type": content_type,
        "size_bytes": len(data),
    }


def _deliver_crm_attendance_event(envelope: dict[str, Any]) -> None:
    event_type=str(envelope.get("event_type") or "")
    if event_type not in {"ATTENDANCE_ENTRY","ATTENDANCE_EXIT","BREAK_START","BREAK_END"}:
        return
    outer_payload=envelope.get("payload") or {}
    payload=outer_payload.get("payload") if isinstance(outer_payload.get("payload"),dict) else outer_payload
    local_person_id=str(payload.get("person_id") or envelope.get("person_id") or "").strip()
    metadata=payload.get('metadata') or {}
    bridge=metadata.get('attendance_sync_bridge') is True
    manual=bridge and metadata.get('attendance_source')=='MANUAL'
    if not local_person_id:
        return
    tenant_id=str(envelope.get("tenant_id") or "")
    shop_id=str(envelope.get("shop_id") or "")
    mapping=store.crm_person_mapping(tenant_id,shop_id,local_person_id)
    if not mapping:
        if bridge: raise ValueError('CRM_MAPPING_REQUIRED')
        return
    if event_type in {"ATTENDANCE_ENTRY","ATTENDANCE_EXIT"} and hasattr(store,"person_attendance_policy"):
        person_policy=store.person_attendance_policy(tenant_id,shop_id,str(mapping["crm_user_id"]))
        if not manual and person_policy and str(person_policy.get("attendanceMode") or "AUTO").upper()=="MANUAL":
            if bridge: raise ValueError('EMPLOYEE_MANUAL_MODE')
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
    if event_type=="ATTENDANCE_ENTRY" and not auto_login_enabled and not manual:
        if bridge: raise ValueError('AUTO_LOGIN_DISABLED')
        return
    if bridge and event_type=='ATTENDANCE_EXIT' and not manual:
        raise ValueError('EXPLICIT_CHECKOUT_REQUIRED')
    if event_type=="ATTENDANCE_EXIT" and not auto_logout_enabled and not manual:
        return
    if event_type in {"BREAK_START","BREAK_END"}:
        # Track loss alone must never mutate CRM break state.
        metadata=payload.get("metadata") or {}
        if metadata.get("crm_confirmed_break") is not True:
            if bridge: raise ValueError('CONFIRMED_BREAK_REQUIRED')
            return
        if event_type=="BREAK_START" and not mapping.get("break_master_id"):
            if bridge: raise ValueError('CRM_BREAK_MAPPING_REQUIRED')
            return
    # Authentication failures occur before the mutation and are safe to retry.
    face_token=_crm_face_token(tenant_id,shop_id,crm_user_id)
    try:
        if event_type=="ATTENDANCE_ENTRY":
            result=crm_client.login_logout_with_face_token({"userId":crm_user_id,"date":crm_date,
                "actualStartTime":crm_time,"actualOffTime":None,
                "loginLocation":location,"logoutLocation":None},face_token)
        elif event_type=="ATTENDANCE_EXIT":
            result=crm_client.login_logout_with_face_token({"userId":crm_user_id,"date":crm_date,
                "actualStartTime":None,"actualOffTime":crm_time,
                "loginLocation":None,"logoutLocation":location},face_token)
        elif event_type=="BREAK_START":
            result=crm_client.start_break(crm_user_id,mapping["break_master_id"],auth_token=face_token)
        else:
            result=crm_client.end_break(crm_user_id,auth_token=face_token)
        if not _crm_mutation_succeeded(result):
            raise RuntimeError("CRM rejected attendance event")
    except Exception as exc:
        if bridge:
            exc.attendance_mutation_attempted=True
        _invalidate_crm_face_token_on_401(tenant_id,shop_id,crm_user_id,exc)
        raise


def _synchronize_attendance_bridge(envelope, *, historical_missing_receipt=False):
    from cloud_portal.attendance_delivery import attendance_payload
    payload=attendance_payload(envelope)
    metadata=payload.get('metadata') or {}
    tenant,shop=str(envelope['tenant_id']),str(envelope.get('shop_id') or '')
    person=str(payload.get('person_id') or '')
    try:
        receipt=store.register_attendance_delivery(
            envelope,
            initial_status=('RECONCILIATION_REQUIRED' if historical_missing_receipt
                            else 'MAPPING_REQUIRED'),
            recovery_reason=('HISTORICAL_RECEIPT_MISSING' if historical_missing_receipt else None),
        )
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    mapping=store.crm_person_mapping(tenant,shop,person)
    if not mapping:
        if receipt.get('status') in {'CRM_CONFIRMED','RECONCILIATION_REQUIRED'}:
            return {'status':receipt['status'],'error_code':'CRM_MAPPING_REQUIRED',
                    'attempts':int(receipt.get('attempts') or 0)}
        store.set_attendance_delivery(envelope,'MAPPING_REQUIRED','CRM_MAPPING_REQUIRED')
        return {'status':'MAPPING_REQUIRED','attempts':int(receipt.get('attempts') or 0)}
    if metadata.get('attendance_source') not in {'MANUAL','RECOGNITION'}:
        raise HTTPException(400,'Invalid attendance source')
    try:
        status,claimed=store.claim_attendance_delivery(envelope,str(mapping['crm_user_id']))
    except ValueError as exc:
        raise HTTPException(409,str(exc)) from exc
    if not claimed:
        return {'status':status}
    try:
        if status!='CRM_CONFIRMED':
            _deliver_crm_attendance_event(envelope)
            status='CRM_CONFIRMED'
            store.set_attendance_delivery(envelope,'CRM_CONFIRMED')
        # These are the same services used by the portal attendance station.
        when=datetime.fromisoformat(str(envelope['event_time']).replace('Z','+00:00'))
        event_type=envelope['event_type']
        if event_type=='ATTENDANCE_ENTRY' and hasattr(store,'touch_attendance_presence'):
            store.touch_attendance_presence(tenant_id=tenant,shop_id=shop,local_person_id=person,
                crm_user_id=str(mapping['crm_user_id']),seen_at=when,camera_id=envelope.get('camera_id'),
                recognition_event_id=envelope['event_id'],checked_in=True)
            store.set_attendance_presence_break(tenant,shop,person,False)
        elif event_type=='ATTENDANCE_EXIT' and hasattr(store,'complete_presence_checkout'):
            store.complete_presence_checkout(tenant,shop,person,True)
        elif event_type in {'BREAK_START','BREAK_END'} and hasattr(store,'set_attendance_presence_break'):
            store.set_attendance_presence_break(tenant,shop,person,event_type=='BREAK_START')
        if hasattr(store,'record_attendance_activity'):
            store.record_attendance_activity({'id':envelope['event_id'],'tenant_id':tenant,'shop_id':shop,
                'crm_user_id':str(mapping['crm_user_id']),'local_person_id':person,
                'activity_type':{'ATTENDANCE_ENTRY':'CHECK_IN','ATTENDANCE_EXIT':'CHECK_OUT',
                                 'BREAK_START':'BREAK_START','BREAK_END':'BREAK_END'}[event_type],
                'occurred_at':when,'source':metadata['attendance_source'],'camera_id':envelope.get('camera_id'),
                'evidence':_action_evidence_manifest(metadata,metadata.get('recognition_event_id')),
                'metadata':metadata})
        store.set_attendance_delivery(envelope,'SUCCEEDED')
        return {'status':'SUCCEEDED'}
    except Exception as exc:
        # Never persist exception text: upstream errors can contain credentials.
        attempted=getattr(exc,'attendance_mutation_attempted',False)
        upstream=exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else None
        known_code=str(exc) if str(exc) in {
            'CRM rejected attendance event','EMPLOYEE_MANUAL_MODE','AUTO_LOGIN_DISABLED',
            'EXPLICIT_CHECKOUT_REQUIRED','CONFIRMED_BREAK_REQUIRED','CRM_BREAK_MAPPING_REQUIRED',
        } else type(exc).__name__
        definitive_rejection=(isinstance(exc,RuntimeError) and str(exc)=='CRM rejected attendance event')
        safe_failure=(definitive_rejection or upstream in {400,401,403,404,409,422,429}
                      or isinstance(exc,(httpx.ConnectError,httpx.ConnectTimeout)))
        uncertain=attempted and not safe_failure
        failure='RECONCILIATION_REQUIRED' if uncertain else 'RETRY'
        # A confirmed remote mutation must only retry local finalization.
        if status=='CRM_CONFIRMED': failure='CRM_CONFIRMED'
        store.set_attendance_delivery(envelope,failure,known_code,retry_seconds=5)
        return {'status':failure,'error_code':known_code}


def _attendance_session_id(person_id: str, when: datetime, action_id: str | None = None) -> str:
    # Sessions can be reopened on the same business day after a real checkout.
    # Never derive the identifier from the date alone.
    suffix=action_id or secrets.token_urlsafe(8)
    return f"attendance-{person_id}-{when.astimezone(timezone.utc).date().isoformat()}-{suffix}"


def _v2_auto_logout_action_id(tenant: str,shop: str,user: str,started: datetime) -> str:
    identity=f"{tenant}|{shop}|{user}|{started.isoformat()}"
    return "auto-logout-"+hashlib.sha256(identity.encode()).hexdigest()[:32]

def _crm_face_login_identity(tenant_code: str, crm_user_id: str, shop_id: str | None=None) -> tuple[str, str]:
    """Return CRM tenant UUID and the user's enrolled face image from the cached directory.

    loginUsingFaceTenant must receive the enrolled Base64 image returned by CRM's
    face-embeddings directory, not Camera Eye recognition evidence. Preserve the CRM
    image bytes exactly: only remove an optional data-URL prefix.
    """
    raw=crm_client.face_embeddings(tenant_code,allow_stale=False)
    users=raw if isinstance(raw,list) else (raw.get("items") or raw.get("data") or [])
    target=str(crm_user_id or "").strip()
    matches=[u for u in users if isinstance(u,dict) and str(u.get('id') or '').strip()==target]
    tenants={str(u.get('tenantId') or '').strip().lower() for u in users if isinstance(u,dict)}
    if not matches: raise RuntimeError('CRM face directory did not return the requested CRM user')
    if len(matches)!=1 or len(tenants)!=1 or '' in tenants:
        raise RuntimeError("CRM directory identity is ambiguous or tenant scope is inconsistent")
    for user in users:
        if not isinstance(user,dict) or str(user.get("id") or "").strip()!=target:
            continue
        crm_tenant_id=str(user.get("tenantId") or "").strip()
        if (user.get('isActive') is False or
            (shop_id and user.get('shopId') and str(user['shopId'])!=str(shop_id)) or
            (user.get('tenantCode') and str(user['tenantCode']).strip().casefold()!=tenant_code.strip().casefold())):
            raise RuntimeError("CRM directory tenant or active identity mismatch")
        if not crm_tenant_id:
            raise RuntimeError("CRM face directory user is missing tenantId")

        sources=_crm_image_sources(user)
        for source in sources:
            image=source.strip()
            if image.startswith(("http://","https://")) or (image.startswith("/") and _decode_crm_face_image(image) is None):
                continue
            if image.startswith("data:") and "," in image:
                image=image.split(",",1)[1].strip()
            if image and _decode_crm_face_image(source) is not None:
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


def _validated_crm_face_login_token(response: Any, crm_user_id: str,
                                    crm_tenant_id: str) -> str:
    """Validate Face Login against the user resolved from the requested tenant directory."""
    if not isinstance(response,dict) or not _crm_face_login_succeeded(response):
        raise RuntimeError("CRM face authentication was rejected")
    user=response.get("user") if isinstance(response.get("user"),dict) else {}
    expected_user=str(crm_user_id or "").strip()
    authenticated_user=str(user.get("id") or "").strip()
    if not expected_user or authenticated_user!=expected_user:
        raise RuntimeError("CRM face authentication identity mismatch")
    # The directory lookup binds this CRM user ID to crm_tenant_id. If Face Login
    # also returns tenantId, reject an explicit disagreement; that field is not
    # required by the currently documented login response contract.
    returned_tenant=str(user.get("tenantId") or "").strip()
    if returned_tenant and returned_tenant!=str(crm_tenant_id).strip():
        raise RuntimeError("CRM face authentication tenant mismatch")
    raw_token=response.get("token")
    token=raw_token.strip() if isinstance(raw_token,str) else ""
    if not token or any(char.isspace() for char in token.removeprefix("Bearer ")):
        raise RuntimeError("CRM face authentication returned no token")
    return token


def _crm_tenant_uuid_for_user(tenant_code: str, crm_user_id: str, shop_id: str | None=None) -> str:
    """Resolve CRM's tenant UUID from the authoritative face directory."""
    raw=crm_client.face_embeddings(tenant_code,allow_stale=False)
    users=raw if isinstance(raw,list) else (raw.get("items") or raw.get("data") or [])
    target=str(crm_user_id or "").strip()
    matches=[u for u in users if isinstance(u,dict) and str(u.get('id') or '').strip()==target]
    tenants={str(u.get('tenantId') or '').strip().lower() for u in users if isinstance(u,dict)}
    if not matches: raise RuntimeError('CRM face directory did not return tenantId for the requested CRM user')
    if len(matches)!=1 or len(tenants)!=1 or '' in tenants:
        raise RuntimeError('CRM directory identity is ambiguous or tenant scope is inconsistent')
    for user in users:
        if not isinstance(user,dict):
            continue
        candidate_user=str(user.get("id") or "").strip()
        candidate_tenant=str(user.get("tenantId") or "").strip()
        if candidate_user==target and candidate_tenant:
            if shop_id and user.get('shopId') and str(user['shopId'])!=str(shop_id):
                raise RuntimeError('CRM directory shop mismatch')
            if user.get("isActive",True) is not True:
                raise RuntimeError("CRM employee is inactive")
            if user.get("tenantCode") and str(user["tenantCode"]).strip().casefold()!=tenant_code.strip().casefold():
                raise RuntimeError("CRM directory tenant code mismatch")
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
    if not isinstance(users,list):
        users=[]
    ids={str(user.get("id") or "").strip() for user in users
         if isinstance(user,dict) and str(user.get("id") or "").strip()}
    logger.info("CRM_TENANT_DIRECTORY tenant_code=%s user_count=%s",tenant_code,len(ids))
    return ids

def _crm_roster_user_ids(rows: Any) -> set[str]:
    if not isinstance(rows,list):
        return set()
    return {str(row.get("userId") or "").strip() for row in rows
            if isinstance(row,dict) and str(row.get("userId") or "").strip()}

def _assert_crm_service_token_scope(tenant_code: str,
                                   target_crm_user_id: str | None = None) -> None:
    """Require positive tenant/user overlap before using a CRM service credential."""
    allowed=_crm_allowed_user_ids(tenant_code)
    now=datetime.now(timezone.utc)
    rows=crm_client.users_roster(now.year,now.month,tenant_code=tenant_code)
    roster_ids=_crm_roster_user_ids(rows)
    if not allowed or not roster_ids:
        logger.error("CRM_TENANT_SCOPE_UNVERIFIED tenant_code=%s directory_users=%s roster_users=%s",
                     tenant_code,len(allowed),len(roster_ids))
        raise RuntimeError("CRM service token tenant scope could not be verified")
    if not (allowed & roster_ids):
        logger.error(
            "CRM_TENANT_SCOPE_MISMATCH tenant_code=%s directory_users=%s roster_users=%s overlap=0",
            tenant_code,len(allowed),len(roster_ids),
        )
        raise RuntimeError("Configured CRM service token is scoped to a different tenant")
    target=str(target_crm_user_id or "").strip()
    if target and (target not in allowed or target not in roster_ids):
        raise RuntimeError("CRM service token scope does not include the requested CRM user")

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


def _action_evidence_manifest(metadata: dict[str, Any] | None, recognition_event_id: str | None = None) -> dict[str, Any]:
    """Build an explicit, filesystem-free evidence manifest for an attendance action."""
    metadata=metadata if isinstance(metadata,dict) else {}
    snapshots=metadata.get("cloud_evidence_snapshots")
    if not isinstance(snapshots,list):
        one=metadata.get("cloud_evidence")
        snapshots=[one] if isinstance(one,dict) and one.get("evidence_id") else []
    clip=metadata.get("cloud_clip") if isinstance(metadata.get("cloud_clip"),dict) else None
    missing=metadata.get("evidence_missing") if isinstance(metadata.get("evidence_missing"),dict) else {}
    snap_count=len(snapshots)
    complete=snap_count>=3 and bool(clip and clip.get("evidence_id"))
    status="COMPLETE" if complete else ("PARTIAL" if snap_count or clip else "UNAVAILABLE")
    if snap_count<3:
        missing.setdefault("snapshots",f"expected_3_received_{snap_count}")
    if not clip:
        missing.setdefault("clip",str(metadata.get("clip_unavailable_reason") or "not_uploaded_or_not_captured"))
    return {"status":status,"snapshots":snapshots[:3],"video":clip,
            "missing":missing,"recognitionEventId":recognition_event_id}


def _evidence_manifest_for_last_recognition(tenant_id: str,shop_id: str,
                                           recognition_event_id: str | None,
                                           action_reason: str) -> dict[str, Any]:
    if not recognition_event_id or not hasattr(store,"get_event"):
        return {"status":"UNAVAILABLE","snapshots":[],"video":None,
                "missing":{"recognition":"last recognition event unavailable",
                           action_reason:"no frame captured at delayed action time"},
                "recognitionEventId":recognition_event_id}
    event=store.get_event(tenant_id,shop_id,recognition_event_id)
    if not event:
        return {"status":"UNAVAILABLE","snapshots":[],"video":None,
                "missing":{"recognition":"last recognition event not found",
                           action_reason:"no frame captured at delayed action time"},
                "recognitionEventId":recognition_event_id}
    envelope=event.get("payload") or {}
    local_event=envelope.get("payload") if isinstance(envelope.get("payload"),dict) else envelope
    metadata=local_event.get("metadata") if isinstance(local_event.get("metadata"),dict) else {}
    manifest=_action_evidence_manifest(metadata,recognition_event_id)
    manifest["missing"].setdefault(action_reason,"last-seen evidence; no frame at delayed action time")
    return manifest

def _auto_attend_recognized_person(envelope: dict[str, Any]) -> None:
    """Immediately face-login a recognized person from an entrance camera."""
    from cloud_portal.attendance_delivery import attendance_payload
    if (attendance_payload(envelope).get('metadata') or {}).get('attendance_sync_bridge') is True:
        # This edge owns the attendance session; recognition is evidence only.
        payload=attendance_payload(envelope)
        tenant=str(envelope.get('tenant_id') or ''); shop=str(envelope.get('shop_id') or '')
        person=str(payload.get('person_id') or '')
        mapping=store.crm_person_mapping(tenant,shop,person) if tenant and shop and person else None
        if mapping and hasattr(store,'touch_attendance_presence'):
            store.touch_attendance_presence(tenant_id=tenant,shop_id=shop,local_person_id=person,
                crm_user_id=str(mapping['crm_user_id']),seen_at=datetime.fromisoformat(str(envelope['event_time']).replace('Z','+00:00')),
                camera_id=envelope.get('camera_id'),recognition_event_id=envelope.get('event_id'),checked_in=None)
        return
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
    presence=None
    if hasattr(store,"touch_attendance_presence"):
        presence=store.touch_attendance_presence(
            tenant_id=tenant_id,shop_id=shop_id,local_person_id=person_id,
            crm_user_id=str(mapping["crm_user_id"]),seen_at=when,camera_id=camera_id,
            recognition_event_id=event_id,camera_zone=camera.get("camera_zone"),checked_in=None,
        )
    # Only an actually open session suppresses a new login. A same-day check-in
    # followed by checkout must allow a legitimate later re-entry.
    if presence and bool(presence.get("checked_in")):
        logger.info("AUTO_ATTENDANCE_SKIPPED event_id=%s person_id=%s reason=session_already_open",event_id,person_id)
        return
    if hasattr(store,"person_attendance_policy"):
        person_policy=store.person_attendance_policy(tenant_id,shop_id,str(mapping["crm_user_id"]))
        if person_policy and str(person_policy.get("attendanceMode") or "AUTO").upper()=="MANUAL":
            logger.info("AUTO_ATTENDANCE_SKIPPED event_id=%s person_id=%s reason=manual_mode",event_id,person_id)
            return
    # Repeat deliveries of a single edge event are rejected at ingestion. Distinct
    # recognition events are suppressed above only while local presence is checked in.
    try:
        authenticated_user_id=str(mapping["crm_user_id"]).strip()
        crm_tenant_id=_crm_tenant_uuid_for_user(tenant_id,authenticated_user_id)
        face_token=_crm_face_token(tenant_id,shop_id,authenticated_user_id)

        crm_date=when.isoformat(timespec="milliseconds").replace("+00:00","Z")
        crm_time=when.strftime("%H:%M:%S")
        attendance_result=crm_client.login_logout_with_face_token({
            "userId":authenticated_user_id,
            "date":crm_date,
            "actualStartTime":crm_time,
        },face_token)
        if not _crm_mutation_succeeded(attendance_result):
            logger.error(
                "CRM_AUTO_ATTENDANCE_REJECTED person_id=%s crm_user_id=%s camera_id=%s",
                person_id,authenticated_user_id,camera_id,
            )
            return
    except Exception as exc:
        _invalidate_crm_face_token_on_401(tenant_id,shop_id,str(mapping["crm_user_id"]),exc)
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
            recognition_event_id=str(envelope.get("event_id") or ""),
            camera_zone=camera.get("camera_zone"),checked_in=True,
        )
        event_metadata=payload.get("metadata") if isinstance(payload.get("metadata"),dict) else {}
        store.record_attendance_activity({
            "id":"activity-"+secrets.token_urlsafe(12),"tenant_id":tenant_id,"shop_id":shop_id,
            "crm_user_id":authenticated_user_id,"local_person_id":person_id,
            "activity_type":"CHECK_IN","occurred_at":when,"source":"CAMERA_EYE",
            "camera_id":camera_id,"reason_code":"FACE_RECOGNITION",
            "evidence":_action_evidence_manifest(event_metadata,str(envelope.get("event_id") or "")),
            "metadata":{"recognition_event_id":str(envelope.get("event_id") or ""),
                        "attendance_session_id":_attendance_session_id(person_id,when,event_id)},
        })
    event_id="auto-attendance-"+secrets.token_urlsafe(12)
    store.record_portal_event({"event_id":event_id,"tenant_id":tenant_id,
        "company_code":envelope.get("company_code"),"shop_id":shop_id,
        "site_id":str(envelope.get("site_id") or shop_id),"edge_id":edge_id,
        "camera_id":camera_id,"event_type":"ATTENDANCE_ENTRY",
        "event_time":when.isoformat(timespec="milliseconds").replace("+00:00","Z"),
        "payload":{"person_id":person_id,"event_type":"ATTENDANCE_ENTRY",
        "metadata":{"attendance_session_id":_attendance_session_id(person_id,when,event_id),
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
    from cloud_portal.attendance_delivery import attendance_payload
    bridge=(attendance_payload(envelope).get('metadata') or {}).get('attendance_sync_bridge') is True
    if bridge and principal.legacy_global:
        raise HTTPException(403,'Attendance synchronization requires a scoped edge credential')
    result=store.ingest_event(envelope)
    if bridge and envelope.get('event_type') in {'ATTENDANCE_ENTRY','ATTENDANCE_EXIT','BREAK_START','BREAK_END'}:
        receipt_before=store.attendance_delivery_receipt(
            str(envelope['tenant_id']),str(envelope.get('shop_id') or ''),str(envelope['event_id']))
        original=store.get_event(str(envelope['tenant_id']),str(envelope.get('shop_id') or ''),str(envelope['event_id']))
        if not original:
            raise HTTPException(409,'Event identity conflicts with an existing scope')
        saved=original['payload']
        # Event IDs are immutable. Never deliver an altered retry payload.
        keys=('tenant_id','shop_id','edge_id','camera_id','event_type','event_time')
        saved_payload=attendance_payload(saved); retry_payload=attendance_payload(envelope)
        if (any(saved.get(k)!=envelope.get(k) for k in keys)
            or saved_payload.get('person_id')!=retry_payload.get('person_id')
            or any((saved_payload.get('metadata') or {}).get(k)!=(retry_payload.get('metadata') or {}).get(k)
                   for k in ('attendance_session_id','attendance_source','predecessor_event_id'))):
            raise HTTPException(409,'Attendance event identity conflict')
        result['attendance_sync']=_synchronize_attendance_bridge(
            saved,historical_missing_receipt=(not result.get('inserted',True) and receipt_before is None))
        return result
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


class AttendanceSyncReconciliation(BaseModel):
    crm_applied: bool
    justification: str = Field(min_length=10,max_length=500)


@app.get('/portal/v1/tenants/{tenant_id}/attendance-sync/{event_id}')
def attendance_sync_receipt(tenant_id: str,event_id: str,
                            principal: PortalPrincipal=Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    _portal_admin(principal)
    receipt=store.attendance_delivery_receipt(tenant_id,principal.shop_id,event_id)
    if not receipt: raise HTTPException(404,'Attendance delivery not found')
    return receipt


@app.get('/portal/v1/tenants/{tenant_id}/attendance-sync/{event_id}/readiness')
def attendance_sync_readiness(tenant_id: str,event_id: str,
                              principal: PortalPrincipal=Depends(require_portal_session)):
    """Return credential-free dispatch blockers for one scoped attendance event."""
    from cloud_portal.attendance_delivery import attendance_payload
    _portal_scope(tenant_id,principal)
    _portal_admin(principal)
    saved=store.get_event(tenant_id,principal.shop_id,event_id)
    if not saved: raise HTTPException(404,'Attendance event not found')
    envelope=saved['payload']; payload=attendance_payload(envelope)
    metadata=payload.get('metadata') or {}
    person=str(payload.get('person_id') or '')
    mapping=store.crm_person_mapping(tenant_id,principal.shop_id,person) if person else None
    policy=(store.person_attendance_policy(tenant_id,principal.shop_id,str(mapping['crm_user_id']))
            if mapping and hasattr(store,'person_attendance_policy') else {}) or {}
    event_type=str(envelope.get('event_type') or '')
    source=str(metadata.get('attendance_source') or '')
    blockers=[]
    if not mapping: blockers.append('CRM_MAPPING_REQUIRED')
    if not getattr(crm_client,'face_attendance_configured',False):
        blockers.append('CRM_FACE_AUTH_NOT_CONFIGURED')
    if not os.getenv('CAMERA_EYE_TOKEN_ENCRYPTION_KEY','').strip():
        blockers.append('TOKEN_ENCRYPTION_KEY_REQUIRED')
    auto_login_enabled=os.getenv('SNAPKEY_CRM_AUTO_LOGIN_ENABLED',
        os.getenv('SNAPKEY_CRM_ATTENDANCE_ENABLED','0')).strip()=='1'
    if event_type=='ATTENDANCE_ENTRY' and source=='RECOGNITION' and not auto_login_enabled:
        blockers.append('AUTO_LOGIN_DISABLED')
    if (event_type in {'ATTENDANCE_ENTRY','ATTENDANCE_EXIT'} and source!='MANUAL'
            and str(policy.get('attendanceMode') or 'AUTO').upper()=='MANUAL'):
        blockers.append('EMPLOYEE_MANUAL_MODE')
    if event_type=='BREAK_START' and mapping and not mapping.get('break_master_id'):
        blockers.append('CRM_BREAK_MAPPING_REQUIRED')
    return {'event_id':event_id,'event_type':event_type,'attendance_source':source,
            'ready':not blockers,'blockers':blockers,
            'receipt':store.attendance_delivery_receipt(tenant_id,principal.shop_id,event_id)}


@app.post('/portal/v1/tenants/{tenant_id}/attendance-sync/{event_id}/reconcile')
def reconcile_attendance_sync(tenant_id: str,event_id: str,request: AttendanceSyncReconciliation,
                              principal: PortalPrincipal=Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    _portal_admin(principal)
    if not store.reconcile_attendance_delivery(tenant_id,principal.shop_id,event_id,
            request.crm_applied,str(principal.user_id),request.justification):
        raise HTTPException(409,'Delivery is not awaiting reconciliation in this shop')
    saved=store.get_event(tenant_id,principal.shop_id,event_id)
    if not saved:
        raise HTTPException(409,'The immutable source event is unavailable')
    result=_synchronize_attendance_bridge(saved['payload'])
    return {'ok':True,**result}


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
    if not value or len(value)>12*1024*1024:
        return None
    if value.startswith("data:image/"):
        if "," not in value:
            return None
        value = value.split(",", 1)[1]
    elif value.startswith(("http://", "https://")):
        return None
    try:
        decoded=base64.b64decode(value, validate=True)
        if source.strip().startswith("/") and not decoded.startswith(b"\xff\xd8\xff"):
            return None
        return decoded
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
    def refresh():
        with _crm_personnel_refresh_lock(tenant_id,shop_id):
            if force or _crm_personnel_refresh_due(tenant_id,shop_id):
                lock=store.crm_enrollment_lock(tenant_id,shop_id) if hasattr(store,"crm_enrollment_lock") else nullcontext(True)
                with lock as acquired:
                    if not acquired:
                        return
                    _refresh_crm_personnel_locked(tenant_id,shop_id)
                    _crm_personnel_last_refresh[(tenant_id,shop_id)]=monotonic_time.monotonic()
    _crm_enrollment_worker.submit((tenant_id,shop_id), refresh)
    # An initial empty mirror is not an authoritative empty roster.
    if ((tenant_id,shop_id) not in _crm_personnel_last_refresh
            and not store.list_cloud_people(tenant_id,shop_id)):
        raise HTTPException(503,"CRM personnel initialization pending; retry shortly")

def _refresh_crm_personnel_locked(tenant_id: str, shop_id: str) -> None:
    try:
        raw=crm_client.face_embeddings(tenant_id)
    except httpx.HTTPError as exc:
        raise HTTPException(502,f"SnapKey CRM personnel lookup failed: {exc}") from exc
    if isinstance(raw,list):
        users=raw
    elif isinstance(raw,dict) and ("success" not in raw or raw["success"] is True):
        users=raw.get("items") if "items" in raw else raw.get("data")
    else:
        users=None
    if not isinstance(users,list) or any(not isinstance(u,dict) or not str(u.get("id") or "").strip() for u in users):
        raise HTTPException(502,"CRM personnel response is not an authoritative user list")
    tenant_uuids={str(u.get("tenantId") or "").strip().lower() for u in users}
    if len({str(u['id']).strip() for u in users}) != len(users):
        raise HTTPException(502,"Duplicate CRM user identity; reconciliation required")
    if users and ("" in tenant_uuids or len(tenant_uuids)!=1):
        raise HTTPException(502,"CRM personnel tenant identity is missing or inconsistent")
    for user in users:
        if "isActive" in user and not isinstance(user["isActive"],bool):
            raise HTTPException(502,"CRM personnel active status is malformed")
        if user.get("tenantCode") and str(user["tenantCode"]).strip().casefold()!=tenant_id.strip().casefold():
            raise HTTPException(502,"CRM personnel tenant code mismatch")
        if user.get("shopId") and str(user["shopId"])!=shop_id:
            raise HTTPException(502,"CRM personnel shop mismatch")
        for field in ("faceImages","profileImage"):
            value=user.get(field)
            if value is not None and not isinstance(value,(str,list)):
                raise HTTPException(502,"CRM enrollment image field is malformed")
            if isinstance(value,list) and any(not isinstance(source,str) for source in value):
                raise HTTPException(502,"CRM enrollment image list is malformed")
        if any(_decode_crm_face_image(source) is None for source in _crm_image_sources(user)):
            raise HTTPException(502,"CRM enrollment image encoding is invalid or unsupported")
    model_key=enrollment_model_key() if any(_crm_image_sources(u) for u in users) else None
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
        mapped=[m for m in store.list_crm_person_mappings(tenant_id,shop_id)
                if str(m['crm_user_id'])==crm_user_id]
        if len(mapped)>1:
            raise HTTPException(502,"Ambiguous CRM identity mapping; reconciliation required")
        existing_person=(store.get_cloud_person(tenant_id,shop_id,mapped[0]['local_person_id']) if mapped else None)
        code_person=next((p for p in store.list_cloud_people(tenant_id,shop_id)
                              if str(p.get("employee_code") or "").strip().lower()==employee_code.strip().lower()),None)
        if existing_person and code_person and existing_person['id']!=code_person['id']:
            raise HTTPException(502,"CRM employee code collision; reconciliation required")
        existing_person=existing_person or code_person
        if existing_person:
            previous_mapping=store.crm_person_mapping(tenant_id,shop_id,str(existing_person["id"]))
            if previous_mapping and str(previous_mapping["crm_user_id"])!=crm_user_id:
                raise HTTPException(502,"CRM employee code changed identity; operator reconciliation required")
        local_person_id=str(existing_person["id"]) if existing_person else "crm-person:"+hashlib.sha256(
            json.dumps([tenant_id,shop_id,crm_user_id],separators=(",",":")).encode()).hexdigest()
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
        active=bool(user.get("isActive",True))
        desired=set()
        for source in _crm_image_sources(user) if active else []:
            raw_image=_decode_crm_face_image(source)
            if not raw_image:
                continue
            digest=hashlib.sha256(json.dumps([tenant_id,str(user["tenantId"]).strip().lower(),shop_id,crm_user_id,local_person_id,
                hashlib.sha256(raw_image).hexdigest(),model_key],separators=(",",":")).encode()).hexdigest()
            face_id="crm-image:"+digest
            desired.add(face_id)
            if store.crm_enrollment_rejected(tenant_id,shop_id,crm_user_id,face_id):
                _crm_enrollment_metrics["rejected_cache_hits"]+=1
                continue
            cached=next((face for face in existing if face["id"]==face_id),None)
            if cached and cached.get('model_key')!=model_key:
                # Preserve legacy bytes; regeneration is a new, separately verified template.
                face_id+=':verified'
                desired.add(face_id)
                cached=next((face for face in existing if face['id']==face_id),None)
            if cached:
                try:
                    validated_embedding(cached["embedding"])
                    if cached.get('model_key')==model_key:
                        _crm_enrollment_metrics["cache_hits"]+=1
                        continue
                    raise RuntimeError('Cached enrollment provenance requires reconciliation')
                except ValueError:
                    raise RuntimeError('Cached enrollment is invalid; re-enrollment required') from None
            try:
                image=cv2.imdecode(np.frombuffer(raw_image,np.uint8),cv2.IMREAD_COLOR)
                if image is None:
                    store.reject_crm_enrollment(tenant_id,shop_id,crm_user_id,face_id,"image_decode_failed")
                    continue
                _crm_enrollment_metrics["inference_count"]+=1
                enroller=_cloud_face_enroller()
                if isinstance(enroller,FaceService) and enroller._app is None:
                    raise RuntimeError("InsightFace enrollment unavailable")
                embedding,quality=enroller.enroll(image)
                if isinstance(enroller,FaceService) and enroller.model_provenance()!=model_key:
                    raise RuntimeError('Enrollment model changed during CRM synchronization')
                embedding=validated_embedding(embedding)
                store.add_cloud_face({"id":face_id,"person_id":local_person_id,
                    "tenant_id":tenant_id,"shop_id":shop_id,"embedding":embedding,
                    "quality":quality,"image_path":None,"model_key":model_key})
            except ValueError:
                _crm_enrollment_metrics["rejected_images"]+=1
                store.reject_crm_enrollment(tenant_id,shop_id,crm_user_id,face_id,"invalid_face_template")
                continue
            except Exception:
                # Model/storage failures must retry, never mark a failed refresh successful.
                raise RuntimeError("CRM enrollment processing unavailable") from None
        if active and "faceImages" not in user and "profileImage" not in user:
            # An omitted enrollment field is not a confirmed revocation.
            desired.update(face["id"] for face in existing if str(face["id"]).startswith("crm-image:"))
        # Withdraw obsolete CRM templates, keeping intentionally enrolled local images.
        # Legacy CRM vectors had no verified model provenance and must be quarantined.
        for face in existing:
            if not face.get('model_key'):
                continue  # Legacy templates remain explicitly UNVERIFIED; never overwritten/backfilled.
            if ((str(face["id"]).startswith("crm-image:") and face["id"] not in desired)
                    or (not str(face["id"]).startswith("crm-image:") and not face.get("image_path"))):
                store.delete_cloud_face(tenant_id,shop_id,local_person_id,face["id"])

    # CRM is authoritative for lifecycle as well. People that disappear from the
    # current CRM face directory must not remain active recognition candidates.
    for person in store.list_cloud_people(tenant_id,shop_id):
        pid=str(person["id"])
        if pid not in seen_local_ids and store.crm_person_mapping(tenant_id,shop_id,pid) and bool(person.get("active")):
            store.update_cloud_person(tenant_id,shop_id,pid,{"active":False})

@app.get("/portal/v1/tenants/{tenant_id}/personnel")
def portal_personnel(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    _refresh_crm_personnel(tenant_id,principal.shop_id)
    edge_people={}
    for edge in store.list_edges(tenant_id,shop_id=principal.shop_id):
        for person in (edge.get("status") or {}).get("personnel") or []:
            edge_people.setdefault(str(person.get("person_id") or ""),[]).append(str(edge.get("edge_id") or ""))
    # The CRM-managed directory must never present local/demo identities as CRM
    # employees. Keep those database rows for historical attendance and evidence.
    mappings={str(m["local_person_id"]):m for m in
              store.list_crm_person_mappings(tenant_id,principal.shop_id)}
    items=[]
    for person in store.list_cloud_people(tenant_id,principal.shop_id):
        pid=str(person["id"])
        mapping=mappings.get(pid)
        if not mapping or not str(mapping.get("crm_user_id") or "").strip():
            continue
        item=dict(person)
        item["person_id"]=pid
        item["edge_ids"]=sorted(set(edge_people.get(pid,[])))
        item["edge_synced"]=bool(item["edge_ids"])
        item["face_enrolled"]=int(item.get("face_count") or 0)>0
        item["crm_user_id"]=str(mapping["crm_user_id"])
        item["crm_mapped"]=True
        item["enrollment_preview_url"]=(f"/portal/v1/tenants/{quote(tenant_id,safe='')}/personnel/{quote(pid,safe='')}/enrollment-image"
                                        if item.get('active') else None)
        items.append(item)
    return {"items":items}

def _enrollment_preview(tenant: str, shop: str, person_id: str):
    person=store.get_cloud_person(tenant,shop,person_id)
    mapping=store.crm_person_mapping(tenant,shop,person_id)
    if not person or not person.get('active') or not mapping:
        raise HTTPException(404,"Enrollment preview unavailable")
    mappings=[m for m in store.list_crm_person_mappings(tenant,shop) if m['crm_user_id']==mapping['crm_user_id']]
    if len(mappings)!=1:
        raise HTTPException(409,"Ambiguous CRM identity")
    try:
        tenant_uuid,source=_crm_face_login_identity(tenant,str(mapping['crm_user_id']),shop)
        raw=_decode_crm_face_image(source)
        image=cv2.imdecode(np.frombuffer(raw,np.uint8),cv2.IMREAD_COLOR) if raw else None
        if image is None: raise ValueError('invalid image')
        ok,encoded=cv2.imencode('.jpg',image)
        if not ok: raise ValueError('invalid image')
    except Exception:
        raise HTTPException(502,"CRM enrollment preview unavailable") from None
    return Response(encoded.tobytes(),media_type='image/jpeg',headers={
        'Cache-Control':'no-store','X-Content-Type-Options':'nosniff','Cross-Origin-Resource-Policy':'same-origin'})


@app.get('/portal/v1/tenants/{tenant_id}/personnel/{person_id}/enrollment-image')
def portal_enrollment_preview(tenant_id: str, person_id: str, principal: PortalPrincipal=Depends(require_portal_session)):
    _portal_scope(tenant_id,principal)
    return _enrollment_preview(tenant_id,principal.shop_id,person_id)


@app.get('/edge/v1/personnel/{person_id}/enrollment-image')
def edge_enrollment_preview(person_id: str, principal: EdgePrincipal=Depends(require_edge_token)):
    if principal.legacy_global: raise HTTPException(403,'Scoped edge credential required')
    return _enrollment_preview(str(principal.tenant_id),str(principal.shop_id),person_id)


@app.get('/portal/v1/tenants/{tenant_id}/personnel-diagnostics')
def personnel_diagnostics(tenant_id: str, principal: PortalPrincipal=Depends(require_portal_session)):
    """Read-only mirror/heartbeat inspection: never refresh CRM or mutate the mirror."""
    _portal_scope(tenant_id,principal)
    mappings=store.list_crm_person_mappings(tenant_id,principal.shop_id)
    try: active_key=enrollment_model_key()
    except (ValueError,OSError): active_key=None
    items=[]
    for person in store.list_cloud_people(tenant_id,principal.shop_id):
        pid=str(person['id']); links=[m for m in mappings if m['local_person_id']==pid]
        faces=store.list_cloud_faces(tenant_id,principal.shop_id,pid)
        edge_reports=[]
        for edge in store.list_edges(tenant_id,shop_id=principal.shop_id):
            for report in (edge.get('status') or {}).get('personnel') or []:
                if report.get('person_id')==pid:
                    edge_reports.append({'edge_id':edge['edge_id'],'face_count':report.get('face_count'),
                        'heartbeat_received_at':edge.get('received_at') or edge.get('last_seen_at'),
                        'model_compatibility':report.get('model_compatibility','UNVERIFIED'),
                        'template_status_counts':report.get('template_status_counts',{}),
                        'templates':report.get('templates',[]),
                        'templates_match':set(report.get('face_ids') or [])=={f['id'] for f in faces},
                        'heartbeat_is_historical':True})
        items.append({'person_id':pid,'crm_user_id':links[0]['crm_user_id'] if len(links)==1 else None,
            'mapping_status':'MAPPED' if len(links)==1 else 'UNMAPPED_OR_AMBIGUOUS',
            'cloud_face_count':len(faces),'enrollment_model_keys':sorted({f['model_key'] for f in faces if f.get('model_key')}),
            'unverified_template_count':sum(not f.get('model_key') for f in faces),'edge_reports':edge_reports,
            **template_diagnostics(faces,active_key)})
    return {'items':items}


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
        from camera_service.attendance_workspace_api import cloud_current_state
        from camera_service.attendance_state import ACTIONS,STATES
        state,_=cloud_current_state(store,tenant_id,principal.shop_id,person_id,str((mapping or {}).get('crm_user_id') or ''))
        actions=[{'START_BREAK':'BREAK_START','END_BREAK':'BREAK_END'}.get(action,action) for action in ACTIONS.get(state,[])]
        return {"candidate":{"state":state,"state_label":STATES[state],"actions":actions,"recognition_event_id":event.get("id"),"person_id":person_id,"full_name":(person or {}).get("full_name") or person_id,
            "employee_code":(person or {}).get("employee_code"),"role":(person or {}).get("role"),"confidence":metadata.get("confidence"),
            "camera_id":camera_id,"edge_id":edge_id,"detected_at":str(event.get("event_time")),"expires_in_seconds":max(0,int(15-age)),
            "crm_mapped":bool(mapping),"break_configured":bool(mapping and mapping.get("break_master_id"))}}
    return {"candidate":None}

@app.post("/portal/v1/tenants/{tenant_id}/attendance-station/action")
def attendance_station_action(tenant_id: str, request: AttendanceStationActionRequest, principal: PortalPrincipal = Depends(require_portal_session)):
    """Execute a CRM-backed manual action for the current recognized person.

    A fresh recognition candidate establishes the employee. All four actions use
    that employee's cached Face Login token, authenticating the current face only
    when the tenant/user token is missing or expired.
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
        try:
            face_token=_crm_face_token(tenant_id,principal.shop_id,authenticated_user_id,
                                       recognition_payload=payload)
        except RuntimeError as exc:
            if "identity mismatch" in str(exc) or "tenant mismatch" in str(exc):
                raise HTTPException(409,"CRM face identity does not match the tenant/person mapping") from exc
            raise HTTPException(502,"CRM face authentication did not return a valid token and identity") from exc
        authenticated_user_id=str(mapping["crm_user_id"])

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
            result=crm_client.start_break(authenticated_user_id,mapping["break_master_id"],
                                          auth_token=face_token)
        else:
            result=crm_client.end_break(authenticated_user_id,auth_token=face_token)
        if not _crm_mutation_succeeded(result):
            logger.warning(
                "CRM_MANUAL_ATTENDANCE_REJECTED action=%s person_id=%s crm_user_id=%s camera_id=%s",
                action,person_id,authenticated_user_id,request.camera_id,
            )
            raise HTTPException(409,"CRM rejected the attendance action")
    except HTTPException:
        raise
    except httpx.HTTPStatusError as exc:
        _invalidate_crm_face_token_on_401(tenant_id,principal.shop_id,authenticated_user_id,exc)
        status=exc.response.status_code if exc.response is not None else 502
        raise HTTPException(502,f"CRM attendance action failed (upstream HTTP {status})") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502,"CRM attendance action failed") from exc
    except Exception as exc:
        logger.exception("CRM_MANUAL_ATTENDANCE_FAILED action=%s person_id=%s camera_id=%s",action,person_id,request.camera_id)
        raise HTTPException(502,"CRM attendance action failed") from exc

    audit_id="manual-"+secrets.token_urlsafe(12)
    if action=="CHECK_IN":
        if hasattr(store,"touch_attendance_presence"):
            store.set_attendance_presence_break(tenant_id,principal.shop_id,person_id,False)
            store.touch_attendance_presence(
                tenant_id=tenant_id,shop_id=principal.shop_id,local_person_id=person_id,
                crm_user_id=authenticated_user_id,seen_at=now,camera_id=request.camera_id,
                camera_zone=camera.get("camera_zone"),recognition_event_id=request.recognition_event_id,
                checked_in=True,
            )
    elif action=="CHECK_OUT":
        if hasattr(store,"complete_presence_checkout"):
            store.complete_presence_checkout(tenant_id,principal.shop_id,person_id,True)
    elif action=="BREAK_START" and hasattr(store,"set_attendance_presence_break"):
        store.set_attendance_presence_break(tenant_id,principal.shop_id,person_id,True)
    elif action=="BREAK_END" and hasattr(store,"set_attendance_presence_break"):
        store.set_attendance_presence_break(tenant_id,principal.shop_id,person_id,False)
    payload_metadata=payload.get("metadata") if isinstance(payload.get("metadata"),dict) else {}
    if hasattr(store,"record_attendance_activity"):
        store.record_attendance_activity({
            "id":audit_id,"tenant_id":tenant_id,"shop_id":principal.shop_id,
            "crm_user_id":authenticated_user_id,"local_person_id":person_id,
            "activity_type":action,"occurred_at":now,"source":"MANUAL",
            "camera_id":request.camera_id,"reason_code":"ATTENDANCE_STATION_ACTION",
            "evidence":_action_evidence_manifest(payload_metadata,request.recognition_event_id),
            "metadata":{"recognition_event_id":request.recognition_event_id,
                        "attendance_session_id":_attendance_session_id(person_id,now,audit_id),
                        "confirmed_by_user_id":principal.user_id},
        })
    canonical_type={"CHECK_IN":"ATTENDANCE_ENTRY","CHECK_OUT":"ATTENDANCE_EXIT",
        "BREAK_START":"BREAK_START","BREAK_END":"BREAK_END"}[action]
    store.record_portal_event({"event_id":audit_id,"tenant_id":tenant_id,"company_code":principal.company_code,"shop_id":principal.shop_id,
        "site_id":str(camera.get("site_id") or principal.shop_id),"edge_id":request.edge_id,"camera_id":request.camera_id,
        "event_type":canonical_type,"event_time":crm_timestamp,"payload":{"person_id":person_id,"event_type":canonical_type,
        "event_time":crm_timestamp,"metadata":{"attendance_session_id":_attendance_session_id(person_id,now,audit_id),
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
    if not crm_client.configured_for_tenant(tenant_id):
        raise HTTPException(503,"SnapKey CRM tenant service token is not configured")
    try:
        _assert_crm_service_token_scope(tenant_id)
        allowed=_crm_allowed_user_ids(tenant_id)
        requested=(user_id or "").strip()
        if requested and requested not in allowed:
            logger.warning("CRM_ROSTER_BLOCKED tenant_code=%s requested_user_id=%s reason=user_not_in_tenant",
                           tenant_id,requested)
            raise HTTPException(404,"CRM user is not part of this tenant")
        rows=crm_client.users_roster(year,month,requested or None,tenant_code=tenant_id)
        if not isinstance(rows,list):
            logger.error("CRM_ROSTER_INVALID_RESPONSE tenant_code=%s response_type=%s",
                         tenant_id,type(rows).__name__)
            raise HTTPException(502,"SnapKey CRM roster returned an invalid response")
        roster_ids=_crm_roster_user_ids(rows)
        overlap=allowed & roster_ids
        if roster_ids and not overlap:
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
    return {"configured":crm_client.face_attendance_configured,
            "roster_configured":crm_client.configured_for_tenant(tenant_id),"base_url":crm_client.base_url,
            "mapping_count":len(store.list_crm_person_mappings(tenant_id,principal.shop_id))}

@app.get("/portal/v1/tenants/{tenant_id}/crm/users")
def crm_users(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    if not crm_client.face_attendance_configured: raise HTTPException(503,"SnapKey CRM face directory is not configured")
    try:
        raw=crm_client.face_embeddings(tenant_id)
        users=raw if isinstance(raw,list) else (raw.get("items") or raw.get("data") or [])
        items=[]
        for user in users:
            if not isinstance(user,dict) or user.get("isActive") is False: continue
            # Resolve identities only from the requested tenant's directory. Never
            # return its biometric fields or use unrelated service-token AllUser data.
            if user.get("tenantCode") and str(user["tenantCode"]).strip().casefold()!=tenant_id.strip().casefold(): continue
            items.append({
                "id":str(user.get("id") or ""),
                "name":user.get("name") or user.get("userName") or "CRM User",
                "user_name":user.get("userName"),
                "employee_code":user.get("employeeCode"),
                "role_name":user.get("roleName"),
                "department_name":user.get("departmentName"),
                "tenant_code":tenant_id,
                "is_admin":bool(user.get("isAdmin")),
            })
        return {"items":[x for x in items if x["id"]]}
    except httpx.HTTPError as exc: raise HTTPException(502,"SnapKey CRM user lookup failed") from exc
    except RuntimeError as exc: raise HTTPException(502,str(exc)) from exc

@app.get("/portal/v1/tenants/{tenant_id}/crm/breaks")
def crm_breaks(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    if not crm_client.configured_for_tenant(tenant_id): raise HTTPException(503,"SnapKey CRM tenant service token is not configured")
    try:
        _assert_crm_service_token_scope(tenant_id)
        return {"items":crm_client.my_breaks(tenant_code=tenant_id)}
    except httpx.HTTPError as exc:
        logger.exception("CRM_BREAK_LOOKUP_HTTP_FAILED tenant_code=%s",tenant_id)
        raise HTTPException(502,"SnapKey CRM break lookup failed") from exc
    except RuntimeError as exc:
        logger.exception("CRM_BREAK_LOOKUP_SCOPE_FAILED tenant_code=%s",tenant_id)
        raise HTTPException(502,str(exc)) from exc

@app.get("/portal/v1/tenants/{tenant_id}/crm/face-embeddings/{employee_code}")
def crm_face_embeddings(tenant_id: str, employee_code: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    # The legacy path calls this employee_code, but CRM expects a tenantCode.
    if employee_code.strip()!=tenant_id.strip():
        raise HTTPException(403,"Face directory must match the authorized tenant")
    if not crm_client.face_attendance_configured: raise HTTPException(503,"SnapKey CRM face directory is not configured")
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
    try:
        face_token=_crm_face_token(tenant_id,principal.shop_id,mapping["crm_user_id"])
        result=crm_client.start_break(mapping["crm_user_id"],mapping["break_master_id"],auth_token=face_token)
        if not _crm_mutation_succeeded(result): raise HTTPException(409,"CRM rejected start-break")
        return {"result":result}
    except httpx.HTTPError as exc:
        _invalidate_crm_face_token_on_401(tenant_id,principal.shop_id,mapping["crm_user_id"],exc)
        raise HTTPException(502,"SnapKey CRM start-break failed") from exc
    except RuntimeError as exc: raise HTTPException(502,str(exc)) from exc

@app.post("/portal/v1/tenants/{tenant_id}/crm/test-break-end/{local_person_id}")
def crm_test_break_end(tenant_id: str, local_person_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_admin(principal)
    _portal_scope(tenant_id, principal)
    mapping=store.crm_person_mapping(tenant_id,principal.shop_id,local_person_id)
    if not mapping: raise HTTPException(404,"CRM person mapping not found")
    try:
        face_token=_crm_face_token(tenant_id,principal.shop_id,mapping["crm_user_id"])
        result=crm_client.end_break(mapping["crm_user_id"],auth_token=face_token)
        if not _crm_mutation_succeeded(result): raise HTTPException(409,"CRM rejected end-break")
        return {"result":result}
    except httpx.HTTPError as exc:
        _invalidate_crm_face_token_on_401(tenant_id,principal.shop_id,mapping["crm_user_id"],exc)
        raise HTTPException(502,"SnapKey CRM end-break failed") from exc
    except RuntimeError as exc: raise HTTPException(502,str(exc)) from exc


@app.get("/portal/v1/tenants/{tenant_id}/summary")
def tenant_summary(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    return store.tenant_summary(tenant_id, shop_id=principal.shop_id)


@app.get("/portal/v1/tenants/{tenant_id}/verification")
def portal_verification(tenant_id: str, limit: int = 100,
                        principal: PortalPrincipal = Depends(require_portal_session)):
    """Read-only diagnostics; never return cross-shop records."""
    _portal_scope(tenant_id, principal)
    from cloud_portal.verification import snapshot
    try:
        return snapshot(store, tenant_id, principal.shop_id, limit)
    except Exception:
        logger.exception("VERIFICATION_SNAPSHOT_FAILED tenant_id=%s shop_id=%s", tenant_id, principal.shop_id)
        raise HTTPException(503, "Verification data unavailable") from None


@app.get("/portal/v1/tenants/{tenant_id}/events")
def tenant_events(tenant_id: str, site_id: str | None = None, event_type: str | None = None, limit: int = 100, alerts_only: bool = False,
                  principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    items = store.list_events(tenant_id, site_id=site_id, event_type=event_type, limit=limit, shop_id=principal.shop_id, alerts_only=alerts_only)
    # Backward-compatible storage implementations may not accept shop_id yet, so enforce
    # the authenticated shop boundary before returning any event to the browser.
    return {"items": [item for item in items if str(item.get("shop_id") or item.get("site_id") or "") == str(principal.shop_id)]}


@app.get("/portal/v1/tenants/{tenant_id}/edges")
def portal_edges(tenant_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    return {"items": store.list_edges(tenant_id, shop_id=principal.shop_id)}


@app.get("/portal/v1/tenants/{tenant_id}/events/{event_id}/evidence")
def portal_event_evidence(tenant_id: str, event_id: str, index: int = 0,
                          principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    path=_resolve_attendance_event_media(tenant_id,principal.shop_id,event_id,"snapshot",index)
    return FileResponse(path,headers={"Cache-Control":"private, no-store"})


@app.get("/portal/v1/tenants/{tenant_id}/events/{event_id}/clip")
def portal_event_clip(tenant_id: str, event_id: str, principal: PortalPrincipal = Depends(require_portal_session)):
    _portal_scope(tenant_id, principal)
    path=_resolve_attendance_event_media(tenant_id,principal.shop_id,event_id,"video")
    return FileResponse(path,media_type="video/webm" if path.suffix.lower()==".webm" else "video/mp4",headers={"Cache-Control":"private, no-store"})


def _resolve_attendance_event_media(tenant_id: str,shop_id: str,event_id: str,
                                    kind: str,index: int=0) -> Path:
    event=store.get_event(tenant_id,shop_id,event_id)
    missing_message="Snapshot not available" if kind=="snapshot" else "Video clip not available"
    if not event:
        raise HTTPException(404,"Event not found")
    envelope=event.get("payload") or {}
    local_event=envelope.get("payload") if isinstance(envelope.get("payload"),dict) else envelope
    metadata=local_event.get("metadata") or {}
    if kind=="snapshot":
        assets=metadata.get("cloud_evidence_snapshots") or []
        if assets:
            asset=next((item for position,item in enumerate(assets)
                        if isinstance(item,dict) and int(item.get("index",position))==index),None)
            evidence=asset.get("evidence") if isinstance(asset,dict) else None
        else:
            evidence=metadata.get("cloud_evidence") if index==0 else None
    else:
        evidence=metadata.get("cloud_clip")
    evidence_id=evidence.get("evidence_id") if isinstance(evidence,dict) else None
    if not evidence_id:
        raise HTTPException(404,missing_message)
    root=Path(os.getenv("SNAPKEY_EVIDENCE_ROOT","/app/data/evidence")).resolve()
    allowed=(root/tenant_id/shop_id/str(event.get("edge_id") or "")).resolve()
    path=(root/str(evidence_id)).resolve()
    if not allowed.is_relative_to(root) or not path.is_relative_to(allowed) or not path.is_file():
        raise HTTPException(404,missing_message)
    return path


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


def _legacy_crm_auto_logout_enabled() -> bool:
    """Return whether legacy background workers may mutate CRM attendance."""
    return os.getenv("SNAPKEY_CRM_AUTO_LOGOUT_ENABLED", "0").strip() == "1"


def _process_automatic_checkout(presence: dict[str, Any], *, now: datetime, reason_code: str,
                                require_camera_health: bool) -> None:
    tenant_id=str(presence["tenant_id"]); shop_id=str(presence["shop_id"])
    person_id=str(presence["local_person_id"]); crm_user_id=str(presence["crm_user_id"])
    if not _legacy_crm_auto_logout_enabled():
        # A row may already have been claimed by another/older worker. Release
        # it without contacting CRM so it cannot remain stuck in claimed state.
        store.complete_presence_checkout(tenant_id,shop_id,person_id,False)
        logger.info("AUTO_CHECKOUT_SKIPPED tenant_id=%s shop_id=%s person_id=%s reason=feature_disabled",
                    tenant_id,shop_id,person_id)
        return
    try:
        attendance_camera_id=presence.get("last_camera_id")
        if require_camera_health and (not attendance_camera_id or not store.attendance_camera_coverage_healthy(
                tenant_id,shop_id,now,camera_id=attendance_camera_id)):
            logger.warning("AUTO_CHECKOUT_DEFERRED tenant_id=%s shop_id=%s person_id=%s reason=camera_coverage_unhealthy",
                           tenant_id,shop_id,person_id)
            store.complete_presence_checkout(tenant_id,shop_id,person_id,False)
            return
        face_token=_crm_face_token(tenant_id,shop_id,crm_user_id)
        result=crm_client.login_logout_with_face_token({
            "userId":crm_user_id,
            "date":now.date().isoformat(),
            "actualOffTime":now.strftime("%H:%M:%S"),
        },face_token)
        if not _crm_mutation_succeeded(result):
            raise RuntimeError("CRM rejected automatic checkout")
        store.complete_presence_checkout(tenant_id,shop_id,person_id,True)
        last_seen=presence["last_seen_at"]
        recognition_id=presence.get("last_recognition_event_id")
        store.record_attendance_activity({
            "id":"activity-"+secrets.token_urlsafe(12),"tenant_id":tenant_id,"shop_id":shop_id,
            "crm_user_id":crm_user_id,"local_person_id":person_id,
            "activity_type":"CHECK_OUT","occurred_at":now,"source":"CAMERA_EYE",
            "camera_id":presence.get("last_camera_id"),"reason_code":reason_code,
            "evidence":_evidence_manifest_for_last_recognition(
                tenant_id,shop_id,recognition_id,"checkout_snapshot"),
            "metadata":{"last_seen_at":last_seen.isoformat() if hasattr(last_seen,"isoformat") else str(last_seen),
                        "last_recognition_event_id":recognition_id,
                        "attendance_session_id":_attendance_session_id(person_id,now,str(recognition_id or "checkout"))},
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
    except Exception as exc:
        _invalidate_crm_face_token_on_401(tenant_id,shop_id,crm_user_id,exc)
        store.complete_presence_checkout(tenant_id,shop_id,person_id,False)
        logger.exception("AUTO_CHECKOUT_FAILED tenant_id=%s shop_id=%s person_id=%s reason=%s",
                         tenant_id,shop_id,person_id,reason_code)




_crm_token_guard=threading.Lock()
_crm_token_locks={}

def _crm_face_token(tenant_id: str, shop_id: str, crm_user_id: str, *,
                    recognition_payload: dict[str, Any] | None = None) -> str:
    key=(tenant_id,shop_id,crm_user_id)
    with _crm_token_guard:
        lock=_crm_token_locks.setdefault(key,threading.Lock())
    with lock:
        return _crm_face_token_locked(tenant_id,shop_id,crm_user_id)


def _crm_face_token_locked(tenant_id: str, shop_id: str, crm_user_id: str) -> str:
    """Reuse one employee's encrypted token; Face Login is the only renewal flow."""
    from cloud_portal.attendance_tokens import decrypt_scoped_token, encrypt_scoped_token, jwt_expiry, usable
    if not hasattr(store,"get_crm_face_token") or not hasattr(store,"save_crm_face_token"):
        raise RuntimeError("Employee CRM authentication requires the PostgreSQL token vault")
    # Check membership against the requested tenant directory even on a cache hit.
    crm_tenant_id=_crm_tenant_uuid_for_user(tenant_id,crm_user_id,shop_id)
    cached=store.get_crm_face_token(tenant_id,shop_id,crm_user_id)
    if cached:
        from cryptography.fernet import InvalidToken
        try:
            token=decrypt_scoped_token(cached["encrypted_token"],tenant_id,shop_id,crm_user_id,crm_tenant_id)
        except (InvalidToken,ValueError,UnicodeDecodeError):
            token=""
        if token and usable(min(cached["expires_at"],jwt_expiry(token))):
            return token
        if hasattr(store,"delete_crm_face_token"):
            store.delete_crm_face_token(tenant_id,shop_id,crm_user_id)
    # The confirmed CRM contract authenticates an enrolled directory image.
    # Camera evidence remains action evidence, never a substitute enrollment.
    directory_tenant,image=_crm_face_login_identity(tenant_id,crm_user_id,shop_id)
    if directory_tenant!=crm_tenant_id:
        raise RuntimeError("CRM face directory tenant changed during authentication")
    response=crm_client.login_using_face_tenant(image,crm_tenant_id)
    token=_validated_crm_face_login_token(response,crm_user_id,crm_tenant_id)
    expires=jwt_expiry(token)
    if not usable(expires):
        raise RuntimeError("CRM face token already expired")
    store.save_crm_face_token(tenant_id,shop_id,crm_user_id,
                              encrypt_scoped_token(token,tenant_id,shop_id,crm_user_id,crm_tenant_id),expires)
    return token


def _invalidate_crm_face_token_on_401(tenant_id: str, shop_id: str,
                                      crm_user_id: str, exc: Exception) -> None:
    """Invalidate only the rejected employee credential; never replay a mutation here."""
    if getattr(getattr(exc,"response",None),"status_code",None)!=401:
        return
    try:
        store.delete_crm_face_token(tenant_id,shop_id,crm_user_id)
    except Exception:
        logger.error("CRM_FACE_TOKEN_INVALIDATION_FAILED tenant_id=%s shop_id=%s crm_user_id=%s",
                     tenant_id,shop_id,crm_user_id)


def _v2_auto_logout(row: dict[str, Any], now: datetime) -> None:
    """Feature gated: must not execute external attendance mutations by default."""
    if os.getenv("CAMERA_EYE_V2_CRM_AUTO_LOGOUT_ENABLED","false").lower()!="true":
        return
    policy=row.get("policy_json") or {}
    if not policy.get("absenceMonitoringEnabled",True):
        return
    if not row.get("checked_in") or row.get("on_break"):
        return
    tenant=str(row["tenant_id"]);shop=str(row["shop_id"]);user=str(row["crm_user_id"])
    if hasattr(store,'has_bridge_attendance') and store.has_bridge_attendance(
            tenant,shop,str(row.get('local_person_id') or '')):
        return  # Bridge sessions require an explicit checkout, not disappearance.
    started=row["last_seen_at"]
    # Policy thresholds drive absence alerts; this one fixed limit is reserved
    # solely for the CRM endpoint's confirmed 60-minute absence contract.
    policy_absence_minutes=int(policy.get("markAbsentAfterMinutes",CRM_AUTO_LOGOUT_MIN_ABSENCE_MINUTES))
    required_absence_minutes=max(policy_absence_minutes,CRM_AUTO_LOGOUT_MIN_ABSENCE_MINUTES)
    if (now-started).total_seconds() < required_absence_minutes*60:
        return
    attendance_camera_id=row.get("last_camera_id")
    coverage_zone=row.get("last_camera_zone") or policy.get("attendanceCameraZone")
    if not attendance_camera_id or not _attendance_coverage_status(
            tenant,shop,now,attendance_camera_id,coverage_zone).get("state")=="HEALTHY":
        return
    if not store.claim_crm_auto_logout(tenant,shop,user,started,
            str(row.get("local_person_id") or ""),attendance_camera_id,
            str(row.get("last_recognition_event_id") or "")):
        return
    crm_mutation_sent=False; crm_confirmation_persisted=False
    try:
        token=_crm_face_token(tenant,shop,user)
        crm_mutation_sent=True
        result=crm_client.auto_logout_with_face_token(
            user,f"AUTO_LOGOUT: Employee not detected by a healthy attendance camera for {CRM_AUTO_LOGOUT_MIN_ABSENCE_MINUTES} minutes",token)
        if not _crm_mutation_succeeded(result):
            # A definitive business rejection is safe to retry with backoff.
            store.release_crm_auto_logout_for_retry(tenant,shop,user,started,"CRM_business_rejection")
            return
        if not store.mark_crm_auto_logout_confirmed(tenant,shop,user,started):
            raise RuntimeError("CRM succeeded but local action confirmation could not be persisted")
        crm_confirmation_persisted=True
        activity_id="v2-"+_v2_auto_logout_action_id(tenant,shop,user,started)
        state=store.finalize_crm_auto_logout_local(tenant,shop,user,started,{
            "id":activity_id,"occurred_at":now,"camera_id":row.get("last_camera_id"),
            "evidence":_evidence_manifest_for_last_recognition(
                tenant,shop,row.get("last_recognition_event_id"),"absence_action_snapshot"),
            "metadata":{"absence_started_at":started.isoformat(),"crm_operation":"auto-logout",
                        "crm_contract_minimum_minutes":CRM_AUTO_LOGOUT_MIN_ABSENCE_MINUTES,
                        "recognition_event_id":row.get("last_recognition_event_id")},
        })
        if state!="SUCCEEDED":
            logger.error("V2_AUTO_LOGOUT_LOCAL_RECONCILIATION_REQUIRED tenant_id=%s shop_id=%s user_id=%s state=%s",
                         tenant,shop,user,state)
    except Exception as exc:
        # A timeout may mean CRM accepted the mutation: operator must reconcile
        # before retrying. Do not blindly resubmit a potentially successful logout.
        try:
            if crm_confirmation_persisted:
                # Leave CRM_CONFIRMED_LOCAL_PENDING intact. The next worker pass
                # performs only the local transaction and never resubmits CRM.
                pass
            elif crm_mutation_sent:
                _invalidate_crm_face_token_on_401(tenant,shop,user,exc)
                store.mark_crm_auto_logout_reconciliation_required(
                    tenant,shop,user,started,"crm_result_or_local_confirmation_ambiguous")
            else:
                store.release_crm_auto_logout_for_retry(tenant,shop,user,started,type(exc).__name__)
        finally:
            logger.exception("V2_AUTO_LOGOUT_NEEDS_RECONCILIATION user_id=%s",user)


def _attendance_coverage_status(tenant: str, shop: str, now: datetime,
                                camera_id: str | None, camera_zone: str | None) -> dict[str, Any]:
    if hasattr(store,"attendance_camera_coverage_status"):
        return store.attendance_camera_coverage_status(
            tenant,shop,now,camera_id=camera_id,camera_zone=camera_zone)
    healthy=bool(store.attendance_camera_coverage_healthy(
        tenant,shop,now,camera_id=camera_id,camera_zone=camera_zone))
    return {"state":"HEALTHY" if healthy else "UNKNOWN",
            "reason":"healthy" if healthy else "coverage_unavailable",
            "cameraZone":camera_zone,"healthyCameraIds":[]}


def _recover_v2_auto_logout_local_finalizations(now: datetime) -> None:
    """Finish local commits after CRM already confirmed; never repeat the CRM call."""
    if not hasattr(store,"list_v2_crm_auto_logout_recovery"):
        return
    for action in store.list_v2_crm_auto_logout_recovery(limit=100):
        tenant=str(action["tenant_id"]);shop=str(action["shop_id"]);user=str(action["crm_user_id"])
        started=action["absence_started_at"]
        activity_id="v2-"+_v2_auto_logout_action_id(tenant,shop,user,started)
        try:
            state=store.finalize_crm_auto_logout_local(tenant,shop,user,started,{
                "id":activity_id,"occurred_at":now,"camera_id":action.get("camera_id"),
                "evidence":_evidence_manifest_for_last_recognition(
                    tenant,shop,action.get("last_recognition_event_id"),"recovered_absence_action_snapshot"),
                "metadata":{"absence_started_at":started.isoformat(),
                            "crm_operation":"auto-logout","recovered_local_commit":True,
                            "recognition_event_id":action.get("last_recognition_event_id")},
            })
            logger.info("V2_AUTO_LOGOUT_RECOVERY user_id=%s state=%s",user,state)
        except Exception:
            logger.exception("V2_AUTO_LOGOUT_RECOVERY_FAILED user_id=%s",user)


def _evaluate_v2_person_absences() -> None:
    """Record person-wise policy transitions; MANUAL sessions remain monitored."""
    if not hasattr(store,"list_v2_attendance_presence"):
        return
    from cloud_portal.person_attendance_rules import PersonAttendancePolicy, evaluate_absence
    now=datetime.now(timezone.utc)
    _recover_v2_auto_logout_local_finalizations(now)
    for row in store.list_v2_attendance_presence(limit=200):
        try:
            tenant=str(row["tenant_id"]); shop=str(row["shop_id"])
            from cloud_portal.policy_resolution import resolve_attendance_policy
            shop_data=(store.attendance_policy(tenant,shop) or {}) if hasattr(store,"attendance_policy") else {}
            resolved=resolve_attendance_policy(row.get("policy_json"),shop_data)
            policy_data=resolved.values
            if not policy_data.get("absenceMonitoringEnabled",True):
                continue
            policy=PersonAttendancePolicy(
                attendance_mode=policy_data.get("attendanceMode","AUTO"),
                presence_update_interval_minutes=policy_data.get("presenceUpdateIntervalMinutes",2),
                out_of_camera_grace_minutes=policy_data.get("outOfCameraGraceMinutes",5),
                max_out_of_camera_occurrences_per_day=policy_data.get("maxOutOfCameraOccurrencesPerDay",5),
                admin_notification_after_minutes=policy_data.get("adminNotificationAfterMinutes",15),
                mark_absent_after_minutes=policy_data.get("markAbsentAfterMinutes",60),
                required_working_minutes=policy_data.get("requiredWorkingMinutes",480),
                timezone=policy_data.get("timezone","Asia/Kolkata"),
            )
            user=str(row["crm_user_id"]); seen=row["last_seen_at"]
            attendance_camera_id=row.get("last_camera_id")
            coverage_zone=row.get("last_camera_zone") or policy_data.get("attendanceCameraZone")
            coverage=_attendance_coverage_status(tenant,shop,now,attendance_camera_id,coverage_zone)
            if hasattr(store,"save_v2_camera_coverage"):
                store.save_v2_camera_coverage(tenant,shop,user,coverage,now)
            from zoneinfo import ZoneInfo as _ZoneInfo
            business_day=now.astimezone(_ZoneInfo(policy.timezone)).date().isoformat()
            episodes=store.count_v2_absence_episodes(tenant,shop,user,business_day)
            # An episode is keyed by its last-seen timestamp, not by each worker tick.
            evaluation=evaluate_absence(
                policy,now=now,last_seen_at=seen,
                checked_in=bool(row["checked_in"]),on_break=bool(row["on_break"]),
                camera_coverage_healthy=coverage.get("state")=="HEALTHY",completed_episodes_today=episodes,
                active_episode_counted=True,
            )
            coverage_transition=("CAMERA_COVERAGE_RESTORED" if coverage.get("state")=="HEALTHY"
                                 else "CAMERA_COVERAGE_UNKNOWN")
            store.record_v2_absence_transition(
                tenant_id=tenant,shop_id=shop,crm_user_id=user,business_date=business_day,
                absence_started_at=seen,transition=coverage_transition,occurred_at=now,
                details={"cameraId":attendance_camera_id,"cameraZone":coverage.get("cameraZone"),
                         "coverageState":coverage.get("state"),"reason":coverage.get("reason"),
                         "healthyCameraIds":coverage.get("healthyCameraIds",[])},
            )
            if coverage.get("state")!="HEALTHY":
                logger.warning("V2_ATTENDANCE_COVERAGE_UNKNOWN tenant_id=%s shop_id=%s user_id=%s zone=%s reason=%s",
                    tenant,shop,user,coverage.get("cameraZone"),coverage.get("reason"))
                continue
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
            # Do not bind the fixed CRM endpoint threshold to markAbsentAfterMinutes:
            # custom policy thresholds may be shorter or longer. Both thresholds
            # must be met before mutation; the CRM contract floor cannot be lowered.
            if (row.get("checked_in") and not row.get("on_break")
                    and policy_data.get("absenceMonitoringEnabled",True)
                    and policy_data.get("absenceAutoLogoutEnabled",True)
                    and evaluation.elapsed_minutes>=max(policy.mark_absent_after_minutes,
                                                        CRM_AUTO_LOGOUT_MIN_ABSENCE_MINUTES)):
                _v2_auto_logout(row,now)
        except Exception:
            logger.exception("V2_ATTENDANCE_EVALUATION_FAILED user_id=%s",row.get("crm_user_id"))


def _evaluate_absence_checkouts() -> None:
    """Evaluate absence and maximum-logoff rules after heartbeats."""
    if not hasattr(store,"claim_due_absence_checkouts"):
        return
    _evaluate_v2_person_absences()
    now=datetime.now(timezone.utc)
    if _legacy_crm_auto_logout_enabled():
        for presence in store.claim_due_max_logoff_checkouts(now,limit=50):
            _process_automatic_checkout(presence,now=now,reason_code="MAX_LOGOFF_REACHED",
                                        require_camera_health=False)
        for presence in store.claim_due_absence_checkouts(now,limit=50):
            if hasattr(store,'has_bridge_attendance') and store.has_bridge_attendance(
                    str(presence['tenant_id']),str(presence['shop_id']),str(presence['local_person_id'])):
                continue
            _process_automatic_checkout(presence,now=now,reason_code="ABSENCE_GRACE_EXCEEDED",
                                        require_camera_health=True)
    else:
        logger.debug("AUTO_CHECKOUT_SCHEDULER_SKIPPED reason=feature_disabled")
    _dispatch_notification_outbox(limit=100)


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


from camera_service.attendance_workspace_api import install_cloud as _install_attendance_workspace
_install_attendance_workspace(app,lambda:store,require_portal_session,_portal_scope,_portal_admin,_resolve_attendance_event_media)


@app.post('/portal/v2/tenants/{tenant_id}/attendance-station/action')
def attendance_station_action_v2(tenant_id:str,request:AttendanceStationActionRequest,principal:PortalPrincipal=Depends(require_portal_session)):
    from camera_service.attendance_workspace_api import apply_cloud_station
    return apply_cloud_station(tenant_id,request,principal,globals())
