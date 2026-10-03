from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any

import httpx


class SnapKeyCrmClient:
    """Server-side SnapKey CRM client. Credentials must never reach edge/browser clients."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: float = 15.0):
        self.base_url=(base_url or os.getenv("SNAPKEY_CRM_API_BASE_URL","https://apis.snapkey.in")).rstrip("/")
        self.token=(token or os.getenv("SNAPKEY_CRM_API_TOKEN","")).strip()
        # The supplied face-login contract currently lives on a separate CRM
        # service endpoint. Keep it configurable instead of hardcoding an IP in
        # the automatic attendance workflow.
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

    def _request(self, method: str, path: str, **kwargs) -> Any:
        headers={**self._headers(),**kwargs.pop("headers",{})}
        with httpx.Client(base_url=self.base_url,timeout=self.timeout,follow_redirects=True) as client:
            response=client.request(method,path,headers=headers,**kwargs)
        response.raise_for_status()
        if not response.content:
            return {"ok":True}
        try:
            return response.json()
        except ValueError:
            return {"ok":True,"text":response.text[:1000]}

    def all_users(self) -> Any:
        # CRM personnel directory. Keep this server-side so the CRM bearer token is never exposed to the browser.
        return self._request("GET","/api/User/AllUser")

    def my_breaks(self) -> Any:
        return self._request("GET","/api/BreakMaster/my-breaks")

    def start_break(self, user_id: str, break_master_id: str) -> Any:
        return self._request("POST","/api/UserBreak/start-break",json={
            "userId":user_id,"breakMasterId":break_master_id,
        })

    def end_break(self, user_id: str) -> Any:
        # API contract supplied by SnapKey uses userId in the query string.
        return self._request("POST","/api/UserBreak/end-break",params={"userId":user_id})

    def face_embeddings(self, tenant_code: str) -> Any:
        # Tenant-scoped CRM personnel/face directory used by Camera Eye.
        return self._request("GET","/api/User/face-embeddings/"+tenant_code)

    def login_using_face_tenant(self, base64_image: str, tenant_id: str) -> Any:
        """Ask CRM to identify/login the person from a current camera face image."""
        image=(base64_image or "").strip()
        crm_tenant_id=(tenant_id or "").strip()
        if not image:
            raise ValueError("base64_image is required")
        if not crm_tenant_id:
            raise ValueError("tenant_id is required")
        # CRM's supplied contract expects raw image Base64, not a data-URL prefix.
        if image.startswith("data:") and "," in image:
            image=image.split(",",1)[1]
        headers={**self._headers(),"Content-Type":"application/json"}
        with httpx.Client(timeout=self.timeout,follow_redirects=True) as client:
            response=client.post(self.face_login_url,headers=headers,json={
                "base64Image":image,
                "tenantId":crm_tenant_id,
            })
        response.raise_for_status()
        if not response.content:
            return {"ok":True}
        try:
            return response.json()
        except ValueError:
            return {"ok":True,"text":response.text[:1000]}

    def users_roster(self, year: int, month: int, user_id: str | None = None) -> Any:
        """Return the CRM roster, which is authoritative for business attendance/history."""
        params={"year":int(year),"month":int(month),"userId":(user_id or "").strip()}
        return self._request("GET","/api/UserRoster/GetUsersRoster",params=params)

    def login_logout(self, payload: dict[str,Any]) -> Any:
        # Retained for explicit/manual attendance operations. Automatic camera
        # attendance uses login_using_face_tenant instead.
        return self._request("POST","/api/UserRoster/LoginLogout",json=payload)


crm_client=SnapKeyCrmClient()