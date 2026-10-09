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
