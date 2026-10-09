from cloud_portal.notifications import NotificationService


def test_email_notification_success_with_mocked_smtp(monkeypatch):
    service = NotificationService()
    service.smtp_host = "smtp.test"
    service.smtp_from = "camera@example.test"
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["connection"] = (host, port, timeout)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def starttls(self):
            sent["starttls"] = True

        def send_message(self, message):
            sent["message"] = message

    monkeypatch.setattr("cloud_portal.notifications.smtplib.SMTP", FakeSMTP)
    result = service.send_email(["admin@example.test"], "Alert", "Test only")
    assert result.delivered is True
    assert sent["connection"] == ("smtp.test", service.smtp_port, 15)
    assert sent["message"]["To"] == "admin@example.test"


def test_whatsapp_success_with_mocked_http(monkeypatch):
    service = NotificationService()
    service.whatsapp_phone_number_id = "phone-test"
    service.whatsapp_access_token = "mock-secret"
    sent = {}

    class Response:
        def raise_for_status(self):
            return None

    def fake_post(url, *, headers, json, timeout):
        sent.update(url=url, headers=headers, payload=json, timeout=timeout)
        return Response()

    monkeypatch.setattr("cloud_portal.notifications.httpx.post", fake_post)
    result = service.send_whatsapp_text(["15550000000"], "Test only")
    assert len(result) == 1 and result[0].delivered is True
    assert sent["payload"]["to"] == "15550000000"
    assert sent["payload"]["text"]["body"] == "Test only"
    assert sent["headers"]["Authorization"] == "Bearer mock-secret"


def test_notification_failures_are_reported_without_raising(monkeypatch):
    service = NotificationService()
    service.smtp_host = "smtp.test"
    service.smtp_from = "camera@example.test"

    def failing_smtp(*_args, **_kwargs):
        raise OSError("mock transport failure")

    monkeypatch.setattr("cloud_portal.notifications.smtplib.SMTP", failing_smtp)
    result = service.send_email(["admin@example.test"], "Alert", "Test")
    assert result.delivered is False
    assert result.detail == "OSError"


def test_notification_test_mode_never_contacts_providers(monkeypatch):
    monkeypatch.setenv('CAMERA_EYE_NOTIFICATIONS_TEST_MODE','true')
    monkeypatch.setattr('cloud_portal.notifications.smtplib.SMTP',lambda *_a,**_k: (_ for _ in ()).throw(AssertionError('SMTP called')))
    monkeypatch.setattr('cloud_portal.notifications.httpx.post',lambda *_a,**_k: (_ for _ in ()).throw(AssertionError('WhatsApp called')))
    service=NotificationService()
    assert service.send_email(['synthetic@example.test'],'Test','Test').detail=='test_mode_no_delivery'
    assert service.send_whatsapp_text(['15550000000'],'Test')[0].delivered is False


def test_provider_exception_message_never_leaks_secrets(monkeypatch,caplog):
    service=NotificationService();service.smtp_host='test';service.smtp_from='test@example.test'
    monkeypatch.setattr('cloud_portal.notifications.smtplib.SMTP',lambda *_a,**_k: (_ for _ in ()).throw(OSError('secret-token-do-not-log')))
    assert service.send_email(['synthetic@example.test'],'Test','Test').delivered is False
    assert 'secret-token-do-not-log' not in caplog.text


def test_employee_recipient_override_and_explicit_empty_channel(monkeypatch):
    from cloud_portal import api
    from unittest.mock import Mock
    store=Mock()
    store.attendance_policy.return_value={'email_recipients':['shop@example.test'],'whatsapp_recipients':['15550000000']}
    store.person_attendance_policy.return_value={'email_recipients':['employee@example.test'],'whatsapp_recipients':[]}
    monkeypatch.setattr(api,'store',store)
    api._notify_cloud_event({'event_id':'synthetic','tenant_id':'tenant','shop_id':'shop','event_type':'ATTENDANCE_POLICY_VIOLATION',
        'payload':{'metadata':{'crm_user_id':'crm-user','reason_code':'GRACE_EXCEEDED'}}})
    call=store.enqueue_notification_delivery.call_args
    assert store.enqueue_notification_delivery.call_count==1
    assert call.args[:5]==('tenant','shop','synthetic','email','employee@example.test')
