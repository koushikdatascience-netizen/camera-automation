"""PostgreSQL contract tests; run with SNAPKEY_TEST_DATABASE_URL to enable."""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from cloud_portal.postgres_storage import PostgresPortalStore


def test_postgres_fresh_schema_outbox_and_auto_logout_recovery():
    dsn=os.getenv("SNAPKEY_TEST_DATABASE_URL","").strip()
    if not dsn:
        if os.getenv("SNAPKEY_REQUIRE_POSTGRES_TESTS")=="1":
            pytest.fail("SNAPKEY_TEST_DATABASE_URL is required in this PostgreSQL test job")
        pytest.skip("SNAPKEY_TEST_DATABASE_URL is not configured; PostgreSQL integration test not run")
    root_url=make_url(dsn)
    if "test" not in str(root_url.database or "").lower():
        message="refusing integration writes unless database name contains 'test'"
        if os.getenv("SNAPKEY_REQUIRE_POSTGRES_TESTS")=="1":
            pytest.fail(message)
        pytest.skip(message)
    schema="camera_eye_test_"+uuid.uuid4().hex[:12]
    database=str(root_url.database).replace('"','""')
    quoted_schema='"'+schema+'"'
    quoted_database='"'+database+'"'
    admin_engine=create_engine(dsn,pool_pre_ping=True)
    store=None
    schema_created=False
    try:
        with admin_engine.begin() as conn:
            conn.execute(text(f'CREATE SCHEMA {quoted_schema}'))
            conn.execute(text(f'ALTER DATABASE {quoted_database} SET search_path TO {quoted_schema}, public'))
        schema_created=True
        store=PostgresPortalStore(dsn)
        now=datetime.now(timezone.utc)
        tenant,shop,user,person="tenant-test","shop-test","user-test","person-test"
        store.upsert_person_attendance_policy(tenant,shop,user,{
            "attendanceMode":"MANUAL","absenceMonitoringEnabled":True,
            "outOfCameraGraceMinutes":5,"adminNotificationAfterMinutes":15,
            "markAbsentAfterMinutes":60,"timezone":"UTC"})
        presence=store.touch_attendance_presence(tenant_id=tenant,shop_id=shop,
            local_person_id=person,crm_user_id=user,seen_at=now-timedelta(hours=1),
            camera_id="cam-test",camera_zone="inside",recognition_event_id="recognition-test",checked_in=True)
        assert presence["checked_in"] is True
        assert presence["last_camera_zone"]=="inside"

        event_id="outbox-integration-test"
        assert store.enqueue_notification_delivery(tenant,shop,event_id,"email","ops@example.test",{
            "subject":"Test","body":"Not a real notification"}) is True
        assert store.enqueue_notification_delivery(tenant,shop,event_id,"email","ops@example.test",{
            "subject":"Test","body":"Not a real notification"}) is False
        deliveries=store.claim_due_notification_deliveries(now+timedelta(seconds=1),limit=5)
        assert len(deliveries)==1
        store.complete_notification_delivery(deliveries[0]["id"],success=False,error="test failure",
            attempts=deliveries[0]["attempts"],retry_after_seconds=60,max_attempts=3)
        assert store.notification_delivery_status(tenant,shop,event_id)=={"PENDING":1}

        started=now-timedelta(hours=1)
        assert store.claim_crm_auto_logout(tenant,shop,user,started) is True
        assert store.mark_crm_auto_logout_confirmed(tenant,shop,user,started) is True
        pending=store.list_v2_crm_auto_logout_recovery()
        assert len(pending)==1
        result=store.finalize_crm_auto_logout_local(tenant,shop,user,started,{
            "id":"auto-logout-test-activity","occurred_at":now,"camera_id":"cam-test",
            "evidence":{"status":"UNAVAILABLE","missing":{"absence":"test"}},
            "metadata":{"crm_operation":"auto-logout"}})
        assert result=="SUCCEEDED"
        assert store.get_person_attendance_presence(tenant,shop,user)["checked_in"] is False
        assert store.list_v2_crm_auto_logout_recovery()==[]
        assert store.get_person_attendance_activity(tenant,shop,user,"auto-logout-test-activity") is not None

        reentry=now-timedelta(hours=1)
        store.touch_attendance_presence(tenant_id=tenant,shop_id=shop,local_person_id=person,
            crm_user_id=user,seen_at=reentry,camera_id="cam-test",camera_zone="inside",
            recognition_event_id="recognition-reentry",checked_in=True)
        ambiguous_started=reentry
        assert store.claim_crm_auto_logout(tenant,shop,user,ambiguous_started,person,"cam-test","recognition-reentry") is True
        store.touch_attendance_presence(tenant_id=tenant,shop_id=shop,local_person_id=person,
            crm_user_id=user,seen_at=reentry+timedelta(minutes=1),camera_id="cam-test",camera_zone="inside",
            recognition_event_id="recognition-during-logout",checked_in=None)
        store.mark_crm_auto_logout_reconciliation_required(tenant,shop,user,ambiguous_started,"test_timeout")
        unresolved=store.list_v2_crm_auto_logout_actions(tenant,shop,user)
        assert unresolved[0]["status"]=="RECONCILIATION_REQUIRED"
        assert unresolved[0]["last_recognition_event_id"]=="recognition-reentry"
        assert store.reconcile_crm_auto_logout_action(tenant,shop,user,ambiguous_started,"CRM_CONFIRMED") is True
        assert store.finalize_crm_auto_logout_local(tenant,shop,user,ambiguous_started,{
            "id":"reconciled-auto-logout","occurred_at":now,"camera_id":"cam-test",
            "evidence":{"status":"UNAVAILABLE"},"metadata":{}})=="SUCCEEDED"
        assert store.get_person_attendance_presence(tenant,shop,user)["checked_in"] is False
        reconciled=store.get_person_attendance_activity(tenant,shop,user,"reconciled-auto-logout")
        assert reconciled["metadata"]["recognitionAdvancedDuringCrmLogout"] is True
    finally:
        if store is not None:
            store.engine.dispose()
        if schema_created:
            with admin_engine.begin() as conn:
                conn.execute(text(f'ALTER DATABASE {quoted_database} RESET search_path'))
                conn.execute(text(f'DROP SCHEMA IF EXISTS {quoted_schema} CASCADE'))
        admin_engine.dispose()
