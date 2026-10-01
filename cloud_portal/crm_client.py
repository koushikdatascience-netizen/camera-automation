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

    def face_embeddings(self, employee_code: str) -> Any:
        return self._request("GET","/api/User/face-embeddings/"+employee_code)

    def login_logout(self, payload: dict[str,Any]) -> Any:
        # Keep the CRM-owned attendance payload explicit. Camera Eye callers build
        # this from a confirmed ENTRY/EXIT event; no edge credential is forwarded.
        return self._request("POST","/api/UserRoster/LoginLogout",json=payload)


crm_client=SnapKeyCrmClient()
