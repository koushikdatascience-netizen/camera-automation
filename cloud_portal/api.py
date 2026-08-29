from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from camera_service.licensing import sign_license_payload
from cloud_portal.storage import PortalStore


store = PortalStore(os.getenv("SNAPKEY_PORTAL_DB", "data/cloud_portal.db"))
app = FastAPI(title="SnapKey Vision AI Portal")


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


def require_edge_token(authorization: str | None = Header(default=None)) -> None:
    expected = os.getenv("SNAPKEY_EDGE_API_TOKEN", "").strip()
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(401, "Invalid edge API token")


@app.get("/health")
def health():
    return {"status": "ok", "service": "snapkey-portal"}


@app.get("/", response_class=HTMLResponse)
@app.get("/portal", response_class=HTMLResponse)
def portal_home():
    return HTMLResponse(
        """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>SnapKey Vision AI Portal</title>
  <style>
    :root {
      --ink: #14213d;
      --muted: #64748b;
      --line: #e2e8f0;
      --surface: #ffffff;
      --soft: #f6f9fc;
      --brand: #0f6fff;
      --brand-dark: #0b3d91;
      --cyan: #00bcd4;
      --success: #16a34a;
      --warning: #d97706;
      --danger: #dc2626;
      --shadow: 0 18px 45px rgba(15, 35, 70, 0.08);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; font-family: Inter, "Segoe UI", Tahoma, sans-serif; }
    body { min-height: 100vh; background: linear-gradient(180deg, #f7fbff 0%, #eef5fb 100%); color: var(--ink); }
    header { background: rgba(255,255,255,.93); border-bottom: 1px solid var(--line); position: sticky; top: 0; backdrop-filter: blur(14px); z-index: 2; }
    .shell { max-width: 1280px; margin: 0 auto; padding: 22px; }
    .topbar { display: flex; align-items: center; justify-content: space-between; gap: 18px; }
    .brand { display: flex; align-items: center; gap: 12px; }
    .mark { width: 44px; height: 44px; border-radius: 8px; display: grid; place-items: center; color: white; font-weight: 800; background: linear-gradient(135deg, var(--brand), var(--cyan)); box-shadow: 0 12px 24px rgba(15,111,255,.22); }
    h1 { font-size: 22px; letter-spacing: 0; }
    .subtitle, .muted { color: var(--muted); font-size: 13px; }
    .toolbar { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
    input, select { height: 40px; border: 1px solid #cbd5e1; border-radius: 8px; padding: 0 12px; background: white; min-width: 190px; }
    button { height: 40px; border: 0; border-radius: 8px; padding: 0 15px; font-weight: 800; color: white; background: linear-gradient(135deg, var(--brand), #0aa9d6); cursor: pointer; }
    main { max-width: 1280px; margin: 0 auto; padding: 24px 22px; }
    .hero { display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 18px; align-items: stretch; margin-bottom: 18px; }
    .panel { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; box-shadow: var(--shadow); padding: 20px; }
    .hero h2 { font-size: 30px; line-height: 1.15; margin-bottom: 10px; }
    .badge { display: inline-flex; align-items: center; gap: 8px; border-radius: 999px; padding: 7px 10px; background: #eaf3ff; color: var(--brand-dark); font-size: 12px; font-weight: 800; margin-bottom: 12px; }
    .metrics { display: grid; grid-template-columns: repeat(4, minmax(0,1fr)); gap: 14px; margin-bottom: 18px; }
    .metric { background: linear-gradient(180deg,#fff,#f8fbff); border: 1px solid var(--line); border-radius: 8px; padding: 16px; }
    .metric span { color: var(--muted); font-size: 12px; font-weight: 800; text-transform: uppercase; }
    .metric strong { display: block; font-size: 26px; margin-top: 7px; }
    .grid { display: grid; grid-template-columns: 1.2fr .8fr; gap: 18px; }
    table { width: 100%; border-collapse: collapse; min-width: 780px; }
    th, td { border-bottom: 1px solid #edf2f7; padding: 13px 14px; text-align: left; font-size: 13px; vertical-align: top; }
    th { color: #475569; background: #f8fafc; font-size: 11px; text-transform: uppercase; }
    .table-wrap { overflow-x: auto; border: 1px solid var(--line); border-radius: 8px; margin-top: 14px; }
    .status { color: var(--success); font-weight: 800; }
    .event-type { color: var(--brand-dark); font-weight: 800; }
    .list { display: grid; gap: 12px; margin-top: 14px; }
    .list div { border: 1px solid var(--line); border-radius: 8px; padding: 12px; background: #fbfdff; }
    @media (max-width: 900px) { .hero, .grid, .metrics { grid-template-columns: 1fr; } .topbar { align-items: flex-start; flex-direction: column; } }
  </style>
</head>
<body>
  <header>
    <div class="shell topbar">
      <div class="brand">
        <div class="mark">SK</div>
        <div>
          <h1>SnapKey Vision AI</h1>
          <div class="subtitle">Cloud command center</div>
        </div>
      </div>
      <div class="toolbar">
        <input id="tenant" value="tenant-demo" placeholder="Tenant ID">
        <input id="site" value="" placeholder="Site ID optional">
        <button onclick="loadPortal()">Refresh</button>
      </div>
    </div>
  </header>
  <main>
    <section class="hero">
      <div class="panel">
        <div class="badge">AI-Powered Platform</div>
        <h2>Multi-tenant visibility for every local edge site.</h2>
        <p class="muted">Track attendance, unknown incidents, crowd events, and object-security alerts while video intelligence remains on the client machine.</p>
      </div>
      <div class="panel">
        <div class="badge">Edge Status</div>
        <p class="muted">Portal API</p>
        <h2 id="portal-status" class="status">Ready</h2>
      </div>
    </section>
    <section class="metrics">
      <div class="metric"><span>Sites</span><strong id="metric-sites">0</strong></div>
      <div class="metric"><span>Edges</span><strong id="metric-edges">0</strong></div>
      <div class="metric"><span>Alerts</span><strong id="metric-alerts">0</strong></div>
      <div class="metric"><span>Attendance</span><strong id="metric-attendance">0</strong></div>
    </section>
    <section class="grid">
      <div class="panel">
        <h2>Latest Events</h2>
        <p class="muted">Tenant-filtered events received from local client machines.</p>
        <div class="table-wrap">
          <table>
            <thead><tr><th>Time</th><th>Type</th><th>Site</th><th>Camera</th><th>Details</th></tr></thead>
            <tbody id="events"><tr><td colspan="5">No events loaded.</td></tr></tbody>
          </table>
        </div>
      </div>
      <div class="panel">
        <h2>Event Mix</h2>
        <p class="muted">Live breakdown for the selected tenant.</p>
        <div id="event-mix" class="list"></div>
      </div>
    </section>
  </main>
  <script>
    async function loadPortal() {
      const tenant = document.getElementById('tenant').value.trim() || 'tenant-demo';
      const site = document.getElementById('site').value.trim();
      const summary = await fetch(`/portal/v1/tenants/${encodeURIComponent(tenant)}/summary`).then(r => r.json());
      const params = new URLSearchParams({ limit: '50' });
      if (site) params.set('site_id', site);
      const events = await fetch(`/portal/v1/tenants/${encodeURIComponent(tenant)}/events?${params}`).then(r => r.json());
      const counts = summary.events || {};
      document.getElementById('metric-sites').textContent = summary.sites || 0;
      document.getElementById('metric-edges').textContent = summary.edges || 0;
      document.getElementById('metric-alerts').textContent = Object.entries(counts).filter(([k]) => k.includes('ALERT') || k.includes('INCIDENT')).reduce((a, [,v]) => a + v, 0);
      document.getElementById('metric-attendance').textContent = (counts.ATTENDANCE_ENTRY || 0) + (counts.ATTENDANCE_EXIT || 0);
      document.getElementById('event-mix').innerHTML = Object.keys(counts).length ? Object.entries(counts).map(([k,v]) => `<div><strong>${k}</strong><br><span class="muted">${v} event(s)</span></div>`).join('') : '<div>No events yet</div>';
      document.getElementById('events').innerHTML = (events.items || []).length ? events.items.map(item => {
        const metadata = item.payload && item.payload.payload && item.payload.payload.metadata ? item.payload.payload.metadata : {};
        const detail = metadata.object_label || metadata.person_count || metadata.snapshot_path || metadata.track_id || '';
        return `<tr><td>${item.event_time || '-'}</td><td class="event-type">${item.event_type}</td><td>${item.site_id}</td><td>${item.camera_id || '-'}</td><td>${detail}</td></tr>`;
      }).join('') : '<tr><td colspan="5">No events found for this tenant.</td></tr>';
    }
    loadPortal().catch(() => { document.getElementById('portal-status').textContent = 'Check API'; });
  </script>
</body>
</html>
        """
    )


@app.post("/edge/v1/events")
def ingest_edge_event(envelope: dict[str, Any], _=Depends(require_edge_token)):
    required = ["schema_version", "tenant_id", "site_id", "edge_id", "event_id", "event_type", "event_time", "payload"]
    missing = [key for key in required if not envelope.get(key)]
    if missing:
        raise HTTPException(400, {"missing": missing})
    if envelope["schema_version"] != "edge.event.v1":
        raise HTTPException(400, "Unsupported event schema")
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
