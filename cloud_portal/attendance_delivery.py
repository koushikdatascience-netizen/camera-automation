"""Delivery receipts for existing edge events, not another attendance ledger.

The edge retains its queue until SUCCEEDED. CLAIMED deliveries are never blindly
replayed after a crash: CRM may have committed before the response was lost.
CRM_CONFIRMED is recoverable without another external mutation.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json

from sqlalchemy import text


def attendance_payload(envelope):
    payload = envelope.get("payload") or {}
    return payload.get("payload") if isinstance(payload.get("payload"), dict) else payload


class AttendanceDeliveryStore:
    @contextmanager
    def _delivery_conn(self):
        with self._conn() as conn:
            if not hasattr(self, "engine"):
                conn.execute("BEGIN IMMEDIATE")
            yield conn

    def _delivery_execute(self, conn, sql, values=None):
        return conn.execute(text(sql) if hasattr(self, "engine") else sql, values or {})

    def initialize_attendance_delivery(self):
        with self._delivery_conn() as conn:
            self._delivery_execute(conn, """CREATE TABLE IF NOT EXISTS edge_attendance_delivery(
                event_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, shop_id TEXT NOT NULL,
                edge_id TEXT NOT NULL, person_id TEXT NOT NULL, crm_user_id TEXT NOT NULL,
                session_id TEXT NOT NULL, predecessor_id TEXT, identity_hash TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                claimed_at TEXT, next_attempt_at TEXT, error_code TEXT, updated_at TEXT NOT NULL)""")
            for column in ('event_type','effective_predecessor_id','registration_reason',
                           'reconciled_by','reconciliation_note','reconciled_at','crm_message'):
                definition="TEXT NOT NULL DEFAULT ''" if column=='event_type' else 'TEXT'
                if hasattr(self,'engine'):
                    self._delivery_execute(conn,f'ALTER TABLE edge_attendance_delivery ADD COLUMN IF NOT EXISTS {column} {definition}')
                elif column not in {r['name'] for r in conn.execute('PRAGMA table_info(edge_attendance_delivery)')}:
                    conn.execute(f'ALTER TABLE edge_attendance_delivery ADD COLUMN {column} {definition}')
            self._delivery_execute(conn,"""UPDATE edge_attendance_delivery SET event_type=COALESCE(
                (SELECT event_type FROM edge_events WHERE id=edge_attendance_delivery.event_id),'')
                WHERE event_type=''""")
            self._delivery_execute(conn,"""UPDATE edge_attendance_delivery
                SET effective_predecessor_id=predecessor_id
                WHERE effective_predecessor_id IS NULL AND predecessor_id IS NOT NULL""")
            self._delivery_execute(conn, """CREATE INDEX IF NOT EXISTS idx_attendance_delivery_person
                ON edge_attendance_delivery(tenant_id,shop_id,person_id,status)""")
            self._delivery_execute(conn, """CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_delivery_session_action
                ON edge_attendance_delivery(tenant_id,shop_id,person_id,session_id,event_type)
                WHERE event_type IN ('ATTENDANCE_ENTRY','ATTENDANCE_EXIT')""")
            self._delivery_execute(conn, "DROP INDEX IF EXISTS idx_attendance_delivery_chain")
            self._delivery_execute(conn, """CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_delivery_chain
                ON edge_attendance_delivery(tenant_id,shop_id,person_id,effective_predecessor_id)
                WHERE effective_predecessor_id IS NOT NULL""")

    def _attendance_identity(self, envelope):
        payload = attendance_payload(envelope)
        metadata = payload.get("metadata") or {}
        identity = {key: envelope.get(key) for key in
                    ("tenant_id", "shop_id", "edge_id", "camera_id", "event_id", "event_type", "event_time")}
        identity.update(person_id=payload.get("person_id"),
                        session_id=metadata.get("attendance_session_id"),
                        predecessor=metadata.get("predecessor_event_id"),
                        source=metadata.get("attendance_source"))
        if not all(identity.get(key) for key in
                   ("tenant_id", "shop_id", "edge_id", "event_id", "event_type",
                    "person_id", "session_id", "source")):
            raise ValueError("Attendance identity, source, and session are required")
        if identity["source"] not in {"MANUAL", "RECOGNITION"}:
            raise ValueError("Invalid attendance source")
        return identity

    def _event_envelope(self, tenant, shop, event_id):
        event = self.get_event(str(tenant), str(shop), str(event_id))
        if not event:
            return None
        value = event.get("payload")
        return value if isinstance(value, dict) else None

    def register_attendance_delivery(self, envelope, *, initial_status="MAPPING_REQUIRED",
                                     recovery_reason=None):
        """Register an immutable cloud receipt before CRM mapping or dispatch.

        Historical attendance predecessors that exist in edge_events but have no
        receipt are quarantined. Their CRM outcome is unknown, so registration must
        never replay them automatically.
        """
        if initial_status not in {"MAPPING_REQUIRED", "RECONCILIATION_REQUIRED"}:
            raise ValueError("Invalid initial attendance delivery status")
        identity = self._attendance_identity(envelope)
        metadata = attendance_payload(envelope).get("metadata") or {}
        original_predecessor = identity["predecessor"]
        effective_predecessor = original_predecessor
        registration_reason = recovery_reason

        if original_predecessor:
            if str(original_predecessor) == str(identity["event_id"]):
                raise ValueError("Attendance event cannot precede itself")
            predecessor_envelope = self._event_envelope(
                identity["tenant_id"], identity["shop_id"], original_predecessor)
            if predecessor_envelope:
                predecessor_type = str(predecessor_envelope.get("event_type") or "")
                predecessor_payload = attendance_payload(predecessor_envelope)
                predecessor_person = str(predecessor_payload.get("person_id") or "")
                if predecessor_person and predecessor_person != str(identity["person_id"]):
                    raise ValueError("Attendance predecessor person conflict")
                if (identity["event_type"] == "ATTENDANCE_ENTRY"
                        and predecessor_type == "PERSON_RECOGNIZED"):
                    effective_predecessor = None
                    registration_reason = "IGNORED_NON_ATTENDANCE_PREDECESSOR"
                elif predecessor_type in {"ATTENDANCE_ENTRY", "ATTENDANCE_EXIT",
                                           "BREAK_START", "BREAK_END"}:
                    if not self.attendance_delivery_receipt(
                            identity["tenant_id"], identity["shop_id"], original_predecessor):
                        self.register_attendance_delivery(
                            predecessor_envelope,
                            initial_status="RECONCILIATION_REQUIRED",
                            recovery_reason="HISTORICAL_RECEIPT_MISSING",
                        )

        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        now = datetime.now(timezone.utc).isoformat()
        args = dict(id=identity["event_id"], tenant=identity["tenant_id"],
                    shop=identity["shop_id"], edge=identity["edge_id"],
                    person=identity["person_id"], session=identity["session_id"],
                    predecessor=original_predecessor, effective=effective_predecessor,
                    digest=digest, type=identity["event_type"], status=initial_status,
                    reason=registration_reason, now=now)
        with self._delivery_conn() as conn:
            duplicate = self._delivery_execute(conn, """SELECT event_id FROM edge_attendance_delivery
                WHERE tenant_id=:tenant AND shop_id=:shop AND person_id=:person AND event_id<>:id
                AND ((session_id=:session AND event_type=:type
                      AND :type IN ('ATTENDANCE_ENTRY','ATTENDANCE_EXIT'))
                     OR (effective_predecessor_id=:effective AND :effective IS NOT NULL))
                LIMIT 1""", args).fetchone()
            if duplicate:
                raise ValueError('Attendance action already has a delivery identity')
            self._delivery_execute(conn, """INSERT INTO edge_attendance_delivery(
                event_id,tenant_id,shop_id,edge_id,person_id,crm_user_id,session_id,
                predecessor_id,effective_predecessor_id,identity_hash,event_type,status,
                registration_reason,updated_at)
                VALUES(:id,:tenant,:shop,:edge,:person,'',:session,:predecessor,:effective,
                       :digest,:type,:status,:reason,:now)
                ON CONFLICT(event_id) DO NOTHING""", args)
            row = self._delivery_execute(conn,
                "SELECT * FROM edge_attendance_delivery WHERE event_id=:id", args).fetchone()
            row = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
            legacy_identity = dict(identity, crm_user_id=str(row.get("crm_user_id") or ""))
            legacy_digest = hashlib.sha256(json.dumps(legacy_identity, sort_keys=True).encode()).hexdigest()
            if row["identity_hash"] not in {digest, legacy_digest}:
                raise ValueError("Attendance event identity conflict")
            if effective_predecessor != original_predecessor:
                self._delivery_execute(conn, """UPDATE edge_attendance_delivery SET
                    effective_predecessor_id=:effective,registration_reason=:reason,updated_at=:now
                    WHERE event_id=:id""", args)
                row["effective_predecessor_id"] = effective_predecessor
                row["registration_reason"] = registration_reason
            return row

    def claim_attendance_delivery(self, envelope, crm_user_id, now=None):
        now = now or datetime.now(timezone.utc)
        payload = attendance_payload(envelope)
        metadata = payload.get("metadata") or {}
        identity = self._attendance_identity(envelope)
        registered = self.register_attendance_delivery(envelope)
        args = dict(id=identity["event_id"], tenant=identity["tenant_id"], shop=identity["shop_id"],
                    edge=identity["edge_id"], person=identity["person_id"], user=str(crm_user_id),
                    session=identity["session_id"], predecessor=registered.get("effective_predecessor_id"),
                    type=envelope['event_type'], now=now.isoformat())
        with self._delivery_conn() as conn:
            if hasattr(self, "engine"):
                lock = int.from_bytes(hashlib.sha256(
                    f"{args['tenant']}|{args['shop']}|{args['person']}".encode()).digest()[:8], "big", signed=True)
                self._delivery_execute(conn, "SELECT pg_advisory_xact_lock(:lock)", {"lock": lock})
                busy=self._delivery_execute(conn,"""SELECT 1 FROM crm_auto_logout_actions
                    WHERE tenant_id=:tenant AND shop_id=:shop AND crm_user_id=:user
                    AND status IN ('IN_FLIGHT','CRM_CONFIRMED_LOCAL_PENDING','RECONCILIATION_REQUIRED') LIMIT 1""",args).fetchone()
                if busy:return 'RECONCILIATION_REQUIRED',False
            duplicate = self._delivery_execute(conn, """SELECT event_id FROM edge_attendance_delivery
                WHERE tenant_id=:tenant AND shop_id=:shop AND person_id=:person AND event_id<>:id
                AND ((session_id=:session AND event_type=:type AND :type IN ('ATTENDANCE_ENTRY','ATTENDANCE_EXIT'))
                     OR (effective_predecessor_id=:predecessor AND :predecessor IS NOT NULL)) LIMIT 1""",args).fetchone()
            if duplicate:
                raise ValueError('Attendance action already has a delivery identity')
            row = self._delivery_execute(conn,
                "SELECT * FROM edge_attendance_delivery WHERE event_id=:id", args).fetchone()
            row = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
            if row.get("crm_user_id") and str(row["crm_user_id"]) != str(crm_user_id):
                raise ValueError("Attendance event identity conflict: CRM user")
            if not row.get("crm_user_id"):
                self._delivery_execute(conn, """UPDATE edge_attendance_delivery SET
                    crm_user_id=:user,status=CASE WHEN status='MAPPING_REQUIRED' THEN 'RETRY' ELSE status END,
                    updated_at=:now WHERE event_id=:id""", args)
                row["crm_user_id"] = str(crm_user_id)
                if row["status"] == "MAPPING_REQUIRED":
                    row["status"] = "RETRY"
            status = row["status"]
            if status == "CLAIMED" and row["claimed_at"] < (now-timedelta(minutes=5)).isoformat():
                self._delivery_execute(conn, """UPDATE edge_attendance_delivery SET
                    status='RECONCILIATION_REQUIRED',error_code='INTERRUPTED_CRM_DELIVERY',updated_at=:now
                    WHERE event_id=:id""", args)
                status = "RECONCILIATION_REQUIRED"
            if status not in {"RETRY", "CRM_CONFIRMED"}:
                return status, False
            if row["next_attempt_at"] and row["next_attempt_at"] > args["now"]:
                return status, False
            if args["predecessor"]:
                predecessor = self._delivery_execute(conn, """SELECT status,tenant_id,shop_id,person_id,session_id,event_type
                    FROM edge_attendance_delivery WHERE event_id=:predecessor""", args).fetchone()
                if not predecessor:
                    return "WAITING_PREDECESSOR", False
                p = dict(predecessor._mapping) if hasattr(predecessor, "_mapping") else dict(predecessor)
                if (p["tenant_id"],p["shop_id"],p["person_id"]) != (args["tenant"],args["shop"],args["person"]):
                    raise ValueError("Attendance predecessor scope conflict")
                if p["status"] != "SUCCEEDED":
                    return "WAITING_PREDECESSOR", False
                allowed={'ATTENDANCE_ENTRY':{'ATTENDANCE_EXIT'},
                         'ATTENDANCE_EXIT':{'ATTENDANCE_ENTRY','BREAK_END','BREAK_START'},
                         'BREAK_START':{'ATTENDANCE_ENTRY','BREAK_END'},
                         'BREAK_END':{'BREAK_START'}}
                if (p['event_type'] not in allowed[envelope['event_type']]
                    or (envelope['event_type']!='ATTENDANCE_ENTRY' and p['session_id']!=args['session'])
                    or (envelope['event_type']=='ATTENDANCE_ENTRY' and p['session_id']==args['session'])):
                    raise ValueError('Attendance session pairing conflict')
            elif envelope['event_type']!='ATTENDANCE_ENTRY':
                legacy_root = (metadata.get('legacy_session_root') is True
                               and metadata.get('attendance_source') == 'MANUAL')
                if not legacy_root:
                    raise ValueError('Attendance session requires a check-in predecessor')
            busy = self._delivery_execute(conn, """SELECT event_id FROM edge_attendance_delivery
                WHERE tenant_id=:tenant AND shop_id=:shop AND person_id=:person
                AND event_id<>:id AND status IN ('CLAIMED','CRM_CONFIRMED','RECONCILIATION_REQUIRED')""", args).fetchone()
            if busy:
                return "WAITING_RECONCILIATION", False
            # Keep CRM_CONFIRMED across local-finalization failures. Finalizers are
            # idempotent, and no CRM call is repeated for this status.
            if status == "RETRY":
                self._delivery_execute(conn, """UPDATE edge_attendance_delivery SET
                    status='CLAIMED',claimed_at=:now,updated_at=:now,attempts=attempts+1
                    WHERE event_id=:id""", args)
            return status, True

    def set_attendance_delivery(self, envelope, status, error_code=None, retry_seconds=0,
                                crm_message=None):
        if status not in {"SUCCEEDED", "CRM_CONFIRMED", "RETRY", "REJECTED", "MAPPING_REQUIRED",
                          "RECONCILIATION_REQUIRED"}:
            raise ValueError("Invalid attendance delivery status")
        now = datetime.now(timezone.utc)
        with self._delivery_conn() as conn:
            self._delivery_execute(conn, """UPDATE edge_attendance_delivery SET status=:status,
                error_code=:error,crm_message=COALESCE(:crm_message,crm_message),
                next_attempt_at=:next,updated_at=:now
                WHERE event_id=:id AND tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge""",
                dict(status=status,error=error_code,crm_message=crm_message,
                     next=(now+timedelta(seconds=retry_seconds)).isoformat(),
                     now=now.isoformat(),id=envelope["event_id"],tenant=envelope["tenant_id"],
                     shop=envelope["shop_id"],edge=envelope["edge_id"]))

    def has_bridge_attendance(self, tenant, shop, person):
        with self._conn() as conn:
            return self._delivery_execute(conn, """SELECT event_id FROM edge_attendance_delivery
                WHERE tenant_id=:tenant AND shop_id=:shop AND person_id=:person LIMIT 1""",
                dict(tenant=tenant,shop=shop,person=person)).fetchone() is not None

    def active_bridge_session(self, tenant, shop, person):
        """Only a confirmed, unclosed session can own a policy checkout."""
        with self._conn() as conn:
            row=self._delivery_execute(conn, """SELECT entry.* FROM edge_attendance_delivery entry
                WHERE entry.tenant_id=:tenant AND entry.shop_id=:shop AND entry.person_id=:person
                AND entry.event_type='ATTENDANCE_ENTRY' AND entry.status='SUCCEEDED'
                AND NOT EXISTS (SELECT 1 FROM edge_attendance_delivery exit
                    WHERE exit.tenant_id=entry.tenant_id AND exit.shop_id=entry.shop_id
                    AND exit.person_id=entry.person_id AND exit.session_id=entry.session_id
                    AND exit.event_type='ATTENDANCE_EXIT' AND exit.status='SUCCEEDED')
                ORDER BY entry.updated_at DESC LIMIT 1""",
                dict(tenant=tenant,shop=shop,person=person)).fetchone()
            if not row:return None
            session=dict(row._mapping) if hasattr(row,'_mapping') else dict(row)
            latest=self._delivery_execute(conn,"""SELECT event_id,status FROM edge_attendance_delivery
                WHERE tenant_id=:tenant AND shop_id=:shop AND person_id=:person
                ORDER BY updated_at DESC LIMIT 1""",dict(tenant=tenant,shop=shop,person=person)).fetchone()
            session['predecessor_event_id']=latest[0]
            session['ready']=latest[1]=='SUCCEEDED'
            heartbeat=self._delivery_execute(conn,"""SELECT status_json,received_at FROM edge_heartbeats
                WHERE tenant_id=:tenant AND shop_id=:shop AND edge_id=:edge
                ORDER BY received_at DESC LIMIT 1""",dict(tenant=tenant,shop=shop,edge=session['edge_id'])).fetchone()
            status=(json.loads(heartbeat[0]) if isinstance(heartbeat[0],str) else heartbeat[0]) if heartbeat else {}
            received=heartbeat[1] if heartbeat else None
            received=datetime.fromisoformat(received) if isinstance(received,str) else received
            session['checkout_capable']=bool(received and 0 <= (datetime.now(timezone.utc)-received).total_seconds() <= 90
                and 'attendance_policy_checkout_v1' in (status or {}).get('capabilities',[]))
            return session

    def attendance_delivery_receipt(self, tenant, shop, event_id):
        with self._conn() as conn:
            row=self._delivery_execute(conn,'SELECT * FROM edge_attendance_delivery WHERE tenant_id=:t AND shop_id=:s AND event_id=:id',
                                       dict(t=tenant,s=shop,id=event_id)).fetchone()
            return (dict(row._mapping) if hasattr(row,'_mapping') else dict(row)) if row else None

    def reconcile_attendance_delivery(self, tenant, shop, event_id, applied, actor, note):
        # An administrator verifies the action in CRM. There is no invented CRM
        # reconciliation/refresh API and no automatic replay of uncertain actions.
        with self._delivery_conn() as conn:
            result=self._delivery_execute(conn,"""UPDATE edge_attendance_delivery SET
                status=:status,next_attempt_at=NULL,error_code=NULL,reconciled_by=:actor,
                reconciliation_note=:note,reconciled_at=:now,updated_at=:now
                WHERE tenant_id=:t AND shop_id=:s AND event_id=:id AND status='RECONCILIATION_REQUIRED'""",
                dict(status='CRM_CONFIRMED' if applied else 'RETRY',actor=actor,note=note,
                     now=datetime.now(timezone.utc).isoformat(),t=tenant,s=shop,id=event_id))
            return result.rowcount==1
