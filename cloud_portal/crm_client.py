from __future__ import annotations

import logging
import os
import time
from typing import Any

import httpx

logger = logging.getLogger("camera_eye.crm")


class SnapKeyCrmClient:
    """Server-side SnapKey CRM client.

    Secrets, face images and authentication tokens are deliberately excluded from
    logs. Tenant/user identifiers and operation names are logged so production
    failures can be traced without exposing credentials or biometric data.
    """

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 15.0):
        self.base_url=(base_url or os.getenv("SNAPKEY_CRM_API_BASE_URL","https://apis.snapkey.in")).rstrip("/")
        self.token=(token or os.getenv("SNAPKEY_CRM_API_TOKEN","")).strip()
        self.face_login_url=os.getenv(
            "SNAPKEY_CRM_FACE_LOGIN_URL",
            self.base_url + "/api/Auth/loginUsingFaceTenant",
        ).strip()
        self.timeout=timeout

    @property
    def configured(self) -> bool:
        return bool(self.token)

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

    @staticmethod
    def _safe_message(result: Any) -> str:
        if not isinstance(result,dict):
            return ""
        value=str(result.get("message") or result.get("status") or "").strip()
        return value[:240]

    def _request(self, method: str, path: str, *, operation: str,
                 auth_token: str | None = None, tenant_code: str | None = None,
                 user_id: str | None = None, **kwargs) -> Any:
        auth_context="face_token" if auth_token else "service_token"
        headers=self._token_headers(auth_token) if auth_token else self._headers()
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

    def all_users(self) -> Any:
        return self._request("GET","/api/User/AllUser",operation="all_users")

    def my_breaks(self, auth_token: str | None = None, user_id: str | None = None) -> Any:
        return self._request("GET","/api/BreakMaster/my-breaks",operation="my_breaks",
                             auth_token=auth_token,user_id=user_id)

    def start_break(self, user_id: str, break_master_id: str, auth_token: str | None = None) -> Any:
        return self._request("POST","/api/UserBreak/start-break",operation="start_break",
            auth_token=auth_token,user_id=user_id,
            json={"userId":user_id,"breakMasterId":break_master_id})

    def end_break(self, user_id: str, auth_token: str | None = None) -> Any:
        return self._request("POST","/api/UserBreak/end-break",operation="end_break",
            auth_token=auth_token,user_id=user_id,params={"userId":user_id})

    def face_embeddings(self, tenant_code: str) -> Any:
        code=(tenant_code or "").strip()
        return self._request("GET","/api/User/face-embeddings/"+code,
                             operation="face_embeddings",tenant_code=code)

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
                    headers={**self._headers(),"Content-Type":"application/json"},
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
            "CRM_FACE_AUTH_RESULT tenant_id=%s success=%s crm_user_id=%s token_present=%s message=%s",
            crm_tenant_id,self.business_success(result),str(crm_user.get("id") or "-"),
            bool(isinstance(result,dict) and result.get("token")),self._safe_message(result),
        )
        return result

    def users_roster(self, year: int, month: int, user_id: str | None = None,
                     auth_token: str | None = None) -> Any:
        uid=(user_id or "").strip()
        params={"year":int(year),"month":int(month),"userId":uid}
        return self._request("GET","/api/UserRoster/GetUsersRoster",operation="users_roster",
                             auth_token=auth_token,user_id=uid or None,params=params)

    def login_logout(self, payload: dict[str,Any]) -> Any:
        user_id=str(payload.get("userId") or "").strip()
        return self._request("POST","/api/UserRoster/LoginLogout",operation="login_logout_service",
                             user_id=user_id,json=payload)

    def login_logout_with_face_token(self, payload: dict[str,Any], face_token: str) -> Any:
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
            "CRM_ATTENDANCE_RESULT action=%s user_id=%s success=%s message=%s",
            action,user_id,self.business_success(result),self._safe_message(result),
        )
        return result


crm_client=SnapKeyCrmClient()
