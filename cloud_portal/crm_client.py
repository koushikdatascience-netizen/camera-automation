from __future__ import annotations

import json
import logging
import os
import time
import threading
from typing import Any

import httpx

logger = logging.getLogger("camera_eye.crm")
logger.setLevel(logging.INFO)


class SnapKeyCrmClient:
    """Server-side SnapKey CRM client.

    Secrets, face images and authentication tokens are deliberately excluded from
    logs. Tenant/user identifiers and operation names are logged so production
    failures can be traced without exposing credentials or biometric data.
    """

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 15.0):
        self.base_url=(base_url or os.getenv("SNAPKEY_CRM_API_BASE_URL","https://apis.snapkey.in")).rstrip("/")
        self.token=(token or os.getenv("SNAPKEY_CRM_API_TOKEN","")).strip()
        raw_tenant_tokens=os.getenv("SNAPKEY_CRM_API_TOKENS_BY_TENANT_JSON","").strip()
        try:
            configured_tenant_tokens=json.loads(raw_tenant_tokens) if raw_tenant_tokens else {}
        except ValueError as exc:
            raise RuntimeError("SNAPKEY_CRM_API_TOKENS_BY_TENANT_JSON must be valid JSON") from exc
        if not isinstance(configured_tenant_tokens,dict):
            raise RuntimeError("SNAPKEY_CRM_API_TOKENS_BY_TENANT_JSON must be a JSON object")
        self.tenant_tokens={}
        for tenant_code,service_token in configured_tenant_tokens.items():
            key=str(tenant_code or "").strip().casefold()
            value=str(service_token or "").strip()
            if not key or not value or key in self.tenant_tokens:
                raise RuntimeError("SNAPKEY_CRM_API_TOKENS_BY_TENANT_JSON has an invalid tenant entry")
            self.tenant_tokens[key]=value
        self.face_login_url=os.getenv(
            "SNAPKEY_CRM_FACE_LOGIN_URL",
            self.base_url + "/api/Auth/loginUsingFaceTenant",
        ).strip()
        self.timeout=timeout
        self.face_directory_ttl_seconds=max(
            1.0,float(os.getenv("SNAPKEY_CRM_FACE_DIRECTORY_TTL_SECONDS","60"))
        )
        self._face_directory_cache: dict[str, tuple[float, Any]]={}
        self._face_directory_lock=threading.RLock()
        self._face_directory_fetch_locks={}

    @property
    def configured(self) -> bool:
        """Whether legacy CRM operations that require a service token are configured."""
        return bool(self.token or self.tenant_tokens)

    def service_token_for_tenant(self, tenant_code: str | None = None) -> str:
        """Select only an exact tenant credential, with the legacy token as fallback.

        Callers performing scoped operations must still verify the token against
        the tenant's CRM directory/roster before allowing a mutation.
        """
        code=str(tenant_code or "").strip().casefold()
        if self.tenant_tokens:
            if not code:
                raise RuntimeError("Tenant code is required when tenant CRM service tokens are configured")
            token=self.tenant_tokens.get(code)
            if not token:
                raise RuntimeError("No CRM service token is configured for this tenant")
        else:
            token=self.token
        if not token:
            raise RuntimeError("No CRM service token is configured for this tenant")
        return token

    def configured_for_tenant(self, tenant_code: str | None = None) -> bool:
        try:
            self.service_token_for_tenant(tenant_code)
            return True
        except RuntimeError:
            return False

    @property
    def face_attendance_configured(self) -> bool:
        """Face attendance uses CRM's public face-directory/login contract, not a static JWT."""
        return bool(self.base_url and self.face_login_url)

    def _headers(self) -> dict[str,str]:
        if not self.token:
            raise RuntimeError("SNAPKEY_CRM_API_TOKEN is not configured")
        return {"Authorization":f"Bearer {self.token}","Accept":"application/json"}

    @staticmethod
    def _token_headers(token: str) -> dict[str,str]:
        value=(token or "").strip()
        if not value:
            raise ValueError("CRM authentication token is required")
        # Face-login token is forwarded verbatim, matching the supplied n8n contract.
        return {"Authorization":value,"Accept":"application/json"}

    @staticmethod
    def business_success(result: Any) -> bool:
        if not isinstance(result,dict):
            return False
        if result.get("success") is True or result.get("ok") is True:
            return True
        return False

    def _request(self, method: str, path: str, *, operation: str,
                 auth_token: str | None = None, service_token: str | None = None,
                 tenant_code: str | None = None,
                 user_id: str | None = None, require_auth: bool = True, **kwargs) -> Any:
        auth_context=("face_token" if auth_token else
                      ("tenant_service_token" if service_token else
                       ("service_token" if require_auth else "public")))
        headers=(self._token_headers(auth_token) if auth_token else
                 ({"Authorization":f"Bearer {service_token}","Accept":"application/json"}
                  if service_token else
                  (self._headers() if require_auth else {"Accept":"application/json"})))
        headers={**headers,**kwargs.pop("headers",{})}
        started=time.monotonic()
        logger.info(
            "CRM_HTTP_REQUEST operation=%s method=%s path=%s auth_context=%s tenant_code=%s user_id=%s",
            operation,method,path,auth_context,tenant_code or "-",user_id or "-",
        )
        try:
            with httpx.Client(base_url=self.base_url,timeout=self.timeout,follow_redirects=True) as client:
                response=client.request(method,path,headers=headers,**kwargs)
            elapsed_ms=int((time.monotonic()-started)*1000)
            logger.info(
                "CRM_HTTP_RESPONSE operation=%s status=%s elapsed_ms=%s auth_context=%s tenant_code=%s user_id=%s",
                operation,response.status_code,elapsed_ms,auth_context,tenant_code or "-",user_id or "-",
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            elapsed_ms=int((time.monotonic()-started)*1000)
            status=exc.response.status_code if exc.response is not None else "unknown"
            logger.warning(
                "CRM_HTTP_ERROR operation=%s status=%s elapsed_ms=%s auth_context=%s tenant_code=%s user_id=%s",
                operation,status,elapsed_ms,auth_context,tenant_code or "-",user_id or "-",
            )
            raise
        except httpx.HTTPError:
            elapsed_ms=int((time.monotonic()-started)*1000)
            logger.exception(
                "CRM_TRANSPORT_ERROR operation=%s elapsed_ms=%s auth_context=%s tenant_code=%s user_id=%s",
                operation,elapsed_ms,auth_context,tenant_code or "-",user_id or "-",
            )
            raise
        if not response.content:
            return {"ok":True}
        try:
            return response.json()
        except ValueError:
            logger.warning("CRM_NON_JSON_RESPONSE operation=%s status=%s",operation,response.status_code)
            return {"ok":True,"text":response.text[:1000]}

    def all_users(self, tenant_code: str | None = None) -> Any:
        token=self.service_token_for_tenant(tenant_code) if tenant_code else None
        return self._request("GET","/api/User/AllUser",operation="all_users",
                             service_token=token,tenant_code=tenant_code)

    def my_breaks(self, auth_token: str | None = None, user_id: str | None = None,
                  tenant_code: str | None = None) -> Any:
        service_token=(self.service_token_for_tenant(tenant_code)
                       if tenant_code and not auth_token else None)
        return self._request("GET","/api/BreakMaster/my-breaks",operation="my_breaks",
                             auth_token=auth_token,service_token=service_token,
                             tenant_code=tenant_code,user_id=user_id)

    def start_break(self, user_id: str, break_master_id: str, auth_token: str) -> Any:
        self._token_headers(auth_token)  # Reject empty employee credentials; no service-token fallback.
        return self._request("POST","/api/UserBreak/start-break",operation="start_break",
            auth_token=auth_token,user_id=user_id,
            json={"userId":user_id,"breakMasterId":break_master_id})

    def end_break(self, user_id: str, auth_token: str) -> Any:
        self._token_headers(auth_token)
        return self._request("POST","/api/UserBreak/end-break",operation="end_break",
            auth_token=auth_token,
            user_id=user_id,params={"userId":user_id})

    def face_embeddings(self, tenant_code: str, *, force_refresh: bool = False, allow_stale: bool = True) -> Any:
        """Return the tenant face directory, refreshing the in-process cache every 60s by default.

        The CRM contract for this endpoint does not require the legacy static JWT.
        A previously successful value is retained as stale-on-error protection so a
        transient CRM outage does not erase tenant/user identity resolution.
        """
        code=(tenant_code or "").strip()
        if not code:
            raise ValueError("tenant_code is required")
        with self._face_directory_lock:
            fetch_lock=self._face_directory_fetch_locks.setdefault(code,threading.Lock())
        with fetch_lock:
            return self._face_embeddings_locked(code,force_refresh,allow_stale)

    def _face_embeddings_locked(self, code: str, force_refresh: bool, allow_stale: bool) -> Any:
        now=time.monotonic()
        with self._face_directory_lock:
            cached=self._face_directory_cache.get(code)
            if cached and not force_refresh and now-cached[0] < self.face_directory_ttl_seconds:
                logger.info("CRM_FACE_DIRECTORY_CACHE_HIT tenant_code=%s age_seconds=%s",
                            code,int(now-cached[0]))
                return cached[1]
        try:
            result=self._request(
                "GET","/api/User/face-embeddings/"+code,
                operation="face_embeddings",tenant_code=code,require_auth=False,
            )
        except Exception:
            with self._face_directory_lock:
                stale=self._face_directory_cache.get(code)
            if stale and allow_stale:
                logger.warning(
                    "CRM_FACE_DIRECTORY_STALE_FALLBACK tenant_code=%s age_seconds=%s",
                    code,int(now-stale[0]),
                )
                return stale[1]
            raise
        with self._face_directory_lock:
            self._face_directory_cache[code]=(time.monotonic(),result)
        logger.info("CRM_FACE_DIRECTORY_REFRESHED tenant_code=%s",code)
        return result

    def login_using_face_tenant(self, base64_image: str, tenant_id: str) -> Any:
        image=(base64_image or "").strip()
        crm_tenant_id=(tenant_id or "").strip()
        if not image:
            raise ValueError("base64_image is required")
        if not crm_tenant_id:
            raise ValueError("tenant_id is required")
        if image.startswith("data:") and "," in image:
            image=image.split(",",1)[1]
        started=time.monotonic()
        logger.info("CRM_FACE_AUTH_REQUEST tenant_id=%s image_present=true",crm_tenant_id)
        try:
            with httpx.Client(timeout=self.timeout,follow_redirects=True) as client:
                response=client.post(
                    self.face_login_url,
                    headers={"Accept":"application/json","Content-Type":"application/json"},
                    json={"base64Image":image,"tenantId":crm_tenant_id},
                )
            elapsed_ms=int((time.monotonic()-started)*1000)
            logger.info("CRM_FACE_AUTH_RESPONSE tenant_id=%s status=%s elapsed_ms=%s",
                        crm_tenant_id,response.status_code,elapsed_ms)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            status=exc.response.status_code if exc.response is not None else "unknown"
            logger.warning("CRM_FACE_AUTH_ERROR tenant_id=%s status=%s",crm_tenant_id,status)
            raise
        except httpx.HTTPError:
            logger.exception("CRM_FACE_AUTH_TRANSPORT_ERROR tenant_id=%s",crm_tenant_id)
            raise
        if not response.content:
            return {"ok":True}
        try:
            result=response.json()
        except ValueError:
            logger.warning("CRM_FACE_AUTH_NON_JSON tenant_id=%s status=%s",crm_tenant_id,response.status_code)
            return {"ok":True,"text":response.text[:1000]}
        crm_user=result.get("user") if isinstance(result,dict) and isinstance(result.get("user"),dict) else {}
        logger.info(
            "CRM_FACE_AUTH_RESULT tenant_id=%s success=%s crm_user_id=%s token_present=%s",
            crm_tenant_id,self.business_success(result),str(crm_user.get("id") or "-"),
            bool(isinstance(result,dict) and result.get("token")),
        )
        return result

    def users_roster(self, year: int, month: int, user_id: str | None = None,
                     auth_token: str | None = None,tenant_code: str | None = None) -> Any:
        uid=(user_id or "").strip()
        params={"year":int(year),"month":int(month),"userId":uid}
        service_token=(self.service_token_for_tenant(tenant_code)
                       if tenant_code and not auth_token else None)
        return self._request("GET","/api/UserRoster/GetUsersRoster",operation="users_roster",
                             auth_token=auth_token,service_token=service_token,
                             tenant_code=tenant_code,user_id=uid or None,params=params)

    def login_logout(self, payload: dict[str,Any], auth_token: str | None = None,
                     tenant_code: str | None = None) -> Any:
        user_id=str(payload.get("userId") or "").strip()
        service_token=(self.service_token_for_tenant(tenant_code)
                       if tenant_code and not auth_token else None)
        return self._request("POST","/api/UserRoster/LoginLogout",operation="login_logout_service",
                             auth_token=auth_token,service_token=service_token,
                             tenant_code=tenant_code,user_id=user_id,json=payload)

    def login_logout_with_face_token(self, payload: dict[str,Any], face_token: str) -> Any:
        self._token_headers(face_token)
        user_id=str(payload.get("userId") or "").strip()
        action="check_in" if "actualStartTime" in payload else ("check_out" if "actualOffTime" in payload else "unknown")
        logger.info(
            "CRM_ATTENDANCE_MUTATION action=%s user_id=%s date=%s actual_start_present=%s actual_off_present=%s",
            action,user_id,str(payload.get("date") or "-"),
            "actualStartTime" in payload,"actualOffTime" in payload,
        )
        result=self._request("POST","/api/UserRoster/LoginLogout",operation="login_logout_"+action,
                             auth_token=face_token,user_id=user_id,json=payload)
        logger.info(
            "CRM_ATTENDANCE_RESULT action=%s user_id=%s success=%s",
            action,user_id,self.business_success(result),
        )
        return result


    def auto_logout_with_face_token(self, user_id: str, remarks: str, face_token: str) -> Any:
        """CRM's confirmed auto-logout endpoint. Do not log or persist the token here."""
        self._token_headers(face_token)
        uid=(user_id or "").strip()
        reason=(remarks or "").strip()
        if not uid or not reason:
            raise ValueError("user_id and remarks are required")
        result=self._request("POST","/api/UserActivity/auto-logout",
                             operation="auto_logout",auth_token=face_token,
                             user_id=uid,json={"userId":uid,"remarks":reason})
        logger.info("CRM_AUTO_LOGOUT_RESULT user_id=%s success=%s",
                    uid,self.business_success(result))
        return result


crm_client=SnapKeyCrmClient()
