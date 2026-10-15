from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any
from urllib.parse import urlsplit, urlunsplit


@dataclass
class OnvifProfile:
    token: str
    name: str
    uri: str
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    encoding: str | None = None

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


class OnvifUnavailable(RuntimeError):
    pass


def _with_credentials(uri: str, username: str | None, password: str | None) -> str:
    if not username:
        return uri
    parts = urlsplit(uri)
    if parts.scheme.lower() != "rtsp":
        return uri
    host = parts.hostname or ""
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    from urllib.parse import quote
    auth = quote(username, safe="")
    if password is not None:
        auth += ":" + quote(password, safe="")
    return urlunsplit((parts.scheme, f"{auth}@{host}{port}", parts.path, parts.query, parts.fragment))


def probe_onvif(host: str, port: int = 80, username: str = "", password: str = "") -> dict[str, Any]:
    """Probe an ONVIF device and return normalized media profiles.

    onvif-zeep is optional: manual RTSP/webcam/file capture remains available without it.
    """
    try:
        from onvif import ONVIFCamera
    except ImportError as exc:
        raise OnvifUnavailable("ONVIF support is not installed. Install optional dependency onvif-zeep.") from exc

    camera = ONVIFCamera(host, int(port), username, password)
    device = camera.create_devicemgmt_service()
    info = device.GetDeviceInformation()
    media = camera.create_media_service()
    profiles: list[OnvifProfile] = []
    for profile in media.GetProfiles() or []:
        token = str(getattr(profile, "token", "") or "")
        name = str(getattr(profile, "Name", "") or token)
        request = media.create_type("GetStreamUri")
        request.ProfileToken = token
        request.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}
        stream = media.GetStreamUri(request)
        uri = _with_credentials(str(getattr(stream, "Uri", "") or ""), username, password)
        video = getattr(profile, "VideoEncoderConfiguration", None)
        resolution = getattr(video, "Resolution", None) if video else None
        rate = getattr(video, "RateControl", None) if video else None
        profiles.append(OnvifProfile(
            token=token, name=name, uri=uri,
            width=int(getattr(resolution, "Width", 0) or 0) or None,
            height=int(getattr(resolution, "Height", 0) or 0) or None,
            fps=float(getattr(rate, "FrameRateLimit", 0) or 0) or None,
            encoding=str(getattr(video, "Encoding", "") or "") or None,
        ))
    return {
        "manufacturer": str(getattr(info, "Manufacturer", "") or ""),
        "model": str(getattr(info, "Model", "") or ""),
        "firmware_version": str(getattr(info, "FirmwareVersion", "") or ""),
        "serial_number": str(getattr(info, "SerialNumber", "") or ""),
        "hardware_id": str(getattr(info, "HardwareId", "") or ""),
        "profiles": [profile.model_dump() for profile in profiles],
    }


def select_profile(profiles: list[dict[str, Any]], purpose: str = "ai") -> dict[str, Any] | None:
    candidates = [p for p in profiles if p.get("uri")]
    if not candidates:
        return None
    def pixels(p):
        return int(p.get("width") or 0) * int(p.get("height") or 0)
    if purpose == "recording":
        return max(candidates, key=lambda p: (pixels(p), float(p.get("fps") or 0)))
    # AI defaults to the lightest usable stream; users can still override manually.
    usable = [p for p in candidates if pixels(p) > 0]
    return min(usable or candidates, key=lambda p: (pixels(p) or 10**12, -(float(p.get("fps") or 0))))
