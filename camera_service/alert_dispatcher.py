from __future__ import annotations


class AlertDispatcher:
    """Provider-neutral alert formatter.

    Production WhatsApp delivery should happen in the cloud through WhatsApp
    Business/Cloud API. The edge agent only creates alert events and can expose
    the exact message that should be delivered.
    """

    def __init__(self, config):
        self.config = config

    def should_alert(self, event_type: str) -> bool:
        if event_type in {"UNKNOWN_INCIDENT", "UNKNOWN_INSIDE_ALERT"}:
            return self.config.send_unknown_inside
        if event_type == "CROWD_ALERT":
            return self.config.send_crowd_alerts
        if event_type in {"BREAK_START", "LONG_BREAK_ALERT"}:
            return self.config.send_long_break_alerts
        return False

    def format_message(self, event: dict) -> str:
        event_type = event.get("event_type", "ALERT")
        camera_id = event.get("camera_id") or "-"
        event_time = event.get("event_time") or "-"
        metadata = event.get("metadata") or {}

        if event_type in {"UNKNOWN_INCIDENT", "UNKNOWN_INSIDE_ALERT"}:
            return f"Alert: Unknown person detected inside. Camera: {camera_id}. Time: {event_time}."
        if event_type == "CROWD_ALERT":
            return (
                f"Alert: Crowd threshold crossed. Camera: {camera_id}. "
                f"People: {metadata.get('person_count')}/{metadata.get('threshold')}. Time: {event_time}."
            )
        if event_type == "BREAK_START":
            return f"Alert: Staff break/removal from view started. Camera: {camera_id}. Time: {event_time}."
        return f"Alert: {event_type}. Camera: {camera_id}. Time: {event_time}."

    def preview_recipients(self) -> list[str]:
        return list(self.config.whatsapp_recipients)
