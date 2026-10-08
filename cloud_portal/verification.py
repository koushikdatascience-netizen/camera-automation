"""Read-only, tenant/shop-scoped verification snapshot for Camera Eye operators."""
from datetime import datetime, timezone
from sqlalchemy import text

def snapshot(store, tenant_id: str, shop_id: str, limit: int = 100):
    if not hasattr(store, "_conn"):
        return {"supported": False, "reason": "PostgreSQL required", "presence": [], "transitions": [], "activities": [], "logout_actions": []}
    n = max(1, min(int(limit), 200))
    with store._conn() as conn:
        presence = conn.execute(text("""
            SELECT p.crm_user_id,p.local_person_id,p.checked_in,p.on_break,p.last_seen_at,
                   p.last_camera_id,p.updated_at,pp.policy_json,
                   c.state AS coverage_state,c.reason AS coverage_reason
            FROM attendance_presence p
            LEFT JOIN person_attendance_policies pp ON pp.tenant_id=p.tenant_id
              AND pp.shop_id=p.shop_id AND pp.crm_user_id=p.crm_user_id
            LEFT JOIN LATERAL (
              SELECT state, reason FROM attendance_camera_coverage
              WHERE tenant_id=p.tenant_id AND shop_id=p.shop_id AND crm_user_id=p.crm_user_id
              ORDER BY checked_at DESC LIMIT 1
            ) c ON TRUE
            WHERE p.tenant_id=:tenant AND p.shop_id=:shop
            ORDER BY p.updated_at DESC LIMIT :limit
        """), {"tenant":tenant_id,"shop":shop_id,"limit":n}).mappings().all()
        transitions = conn.execute(text("""
            SELECT crm_user_id,transition,occurred_at,absence_started_at,details_json
            FROM person_attendance_transitions WHERE tenant_id=:tenant AND shop_id=:shop
            ORDER BY occurred_at DESC LIMIT :limit
        """), {"tenant":tenant_id,"shop":shop_id,"limit":n}).mappings().all()
        activities = conn.execute(text("""
            SELECT id,crm_user_id,activity_type,occurred_at,reason_code,source,camera_id,evidence_json
            FROM attendance_activity WHERE tenant_id=:tenant AND shop_id=:shop
            ORDER BY occurred_at DESC LIMIT :limit
        """), {"tenant":tenant_id,"shop":shop_id,"limit":n}).mappings().all()
        actions = conn.execute(text("""
            SELECT crm_user_id,status,absence_started_at,attempts,last_error,completed_at
            FROM crm_auto_logout_actions WHERE tenant_id=:tenant AND shop_id=:shop
            ORDER BY absence_started_at DESC LIMIT :limit
        """), {"tenant":tenant_id,"shop":shop_id,"limit":n}).mappings().all()
    def serialize(rows):
        return [{k:(v.isoformat() if isinstance(v,datetime) else v) for k,v in dict(row).items()} for row in rows]
    return {"supported":True,"as_of":datetime.now(timezone.utc).isoformat(),
            "presence":serialize(presence),"transitions":serialize(transitions),
            "activities":serialize(activities),"logout_actions":serialize(actions)}
