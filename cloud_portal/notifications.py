from __future__ import annotations

import logging
import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Iterable

import httpx

logger = logging.getLogger("camera_eye.notifications")


@dataclass(frozen=True)
class NotificationResult:
    channel: str
    delivered: bool
    detail: str


class NotificationService:
    """Cloud-only notification adapter for SMTP email and WhatsApp Business Cloud API.

    Secrets stay in environment variables. Event logs contain provider status only,
    never SMTP passwords, WhatsApp access tokens, face images, or CRM JWTs.
    """

    def __init__(self) -> None:
        self.smtp_host = os.getenv("SNAPKEY_SMTP_HOST", "").strip()
        self.smtp_port = int(os.getenv("SNAPKEY_SMTP_PORT", "587"))
        self.smtp_user = os.getenv("SNAPKEY_SMTP_USER", "").strip()
        self.smtp_password = os.getenv("SNAPKEY_SMTP_PASSWORD", "")
        self.smtp_from = os.getenv("SNAPKEY_SMTP_FROM", self.smtp_user).strip()
        self.smtp_tls = os.getenv("SNAPKEY_SMTP_STARTTLS", "1").strip() == "1"
        self.whatsapp_phone_number_id = os.getenv("SNAPKEY_WHATSAPP_PHONE_NUMBER_ID", "").strip()
        self.whatsapp_access_token = os.getenv("SNAPKEY_WHATSAPP_ACCESS_TOKEN", "").strip()
        self.whatsapp_api_version = os.getenv("SNAPKEY_WHATSAPP_API_VERSION", "v23.0").strip()

    def send_email(self, recipients: Iterable[str], subject: str, body: str) -> NotificationResult:
        recipients = [x.strip() for x in recipients if x and x.strip()]
        if not recipients:
            return NotificationResult("email", False, "no_recipients")
        if not self.smtp_host or not self.smtp_from:
            return NotificationResult("email", False, "smtp_not_configured")
        message = EmailMessage()
        message["From"] = self.smtp_from
        message["To"] = ", ".join(recipients)
        message["Subject"] = subject
        message.set_content(body)
        try:
            with smtplib.SMTP(self.smtp_host, self.smtp_port, timeout=15) as client:
                if self.smtp_tls:
                    client.starttls()
                if self.smtp_user:
                    client.login(self.smtp_user, self.smtp_password)
                client.send_message(message)
            logger.info("NOTIFICATION_SENT channel=email recipient_count=%s", len(recipients))
            return NotificationResult("email", True, "sent")
        except Exception as exc:
            logger.exception("NOTIFICATION_FAILED channel=email error_type=%s", type(exc).__name__)
            return NotificationResult("email", False, type(exc).__name__)

    def send_whatsapp_text(self, recipients: Iterable[str], body: str) -> list[NotificationResult]:
        recipients = [x.strip() for x in recipients if x and x.strip()]
        if not recipients:
            return [NotificationResult("whatsapp", False, "no_recipients")]
        if not self.whatsapp_phone_number_id or not self.whatsapp_access_token:
            return [NotificationResult("whatsapp", False, "whatsapp_not_configured")]
        url = (
            f"https://graph.facebook.com/{self.whatsapp_api_version}/"
            f"{self.whatsapp_phone_number_id}/messages"
        )
        headers = {"Authorization": f"Bearer {self.whatsapp_access_token}", "Content-Type": "application/json"}
        results: list[NotificationResult] = []
        for recipient in recipients:
            payload = {"messaging_product": "whatsapp", "to": recipient,
                       "type": "text", "text": {"preview_url": False, "body": body[:4096]}}
            try:
                response = httpx.post(url, headers=headers, json=payload, timeout=15.0)
                response.raise_for_status()
                logger.info("NOTIFICATION_SENT channel=whatsapp recipient_suffix=%s", recipient[-4:])
                results.append(NotificationResult("whatsapp", True, "sent"))
            except Exception as exc:
                logger.exception("NOTIFICATION_FAILED channel=whatsapp recipient_suffix=%s error_type=%s",
                                 recipient[-4:], type(exc).__name__)
                results.append(NotificationResult("whatsapp", False, type(exc).__name__))
        return results
