import os
import json
import argparse
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

def configure_frozen_ca_bundle() -> None:
    """Point HTTPS clients at the bundled certifi CA file in PyInstaller builds."""
    if not getattr(sys, "frozen", False):
        return
    bundle_base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    candidates = [
        bundle_base / "certifi" / "cacert.pem",
        Path(sys.executable).resolve().parent / "_internal" / "certifi" / "cacert.pem",
        Path(sys.executable).resolve().parent / "certifi" / "cacert.pem",
    ]
    for candidate in candidates:
        if candidate.exists():
            os.environ.setdefault("REQUESTS_CA_BUNDLE", str(candidate))
            os.environ.setdefault("SSL_CERT_FILE", str(candidate))
            return


configure_frozen_ca_bundle()

_LOG_STREAM = None
_INSTANCE_HANDLE = None


def _runtime_log_dir() -> Path:
    base = os.environ.get("CAMERA_AUTOMATION_HOME")
    if base:
        root = Path(base)
    else:
        root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "MadhushalaCameraAI"
    log_dir = root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def ensure_console_streams() -> None:
    """Windowed PyInstaller apps have no console streams; Uvicorn logging expects them."""
    global _LOG_STREAM
    if sys.stdout is not None and sys.stderr is not None:
        return
    log_path = _runtime_log_dir() / "launcher.log"
    _LOG_STREAM = log_path.open("a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = _LOG_STREAM
    if sys.stderr is None:
        sys.stderr = _LOG_STREAM


def env_bool(name: str, default: bool = True) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def wait_for_health(url: str, timeout_seconds: float = 60.0) -> bool:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if 200 <= response.status < 300:
                    return True
        except (OSError, urllib.error.URLError):
            time.sleep(0.5)
    return False


def check_port(host: str, port: int, timeout: float = 60.0) -> bool:
    return wait_for_health(f"http://{host}:{port}/health", timeout)


def open_browser(url: str) -> None:
    webbrowser.open(url)


def open_browser_when_ready(host: str, port: int) -> None:
    health_url = f"http://{host}:{port}/health"
    setup_url = f"http://{host}:{port}/setup"
    if wait_for_health(health_url):
        try:
            open_browser(setup_url)
        except Exception as exc:
            print(f"Failed to open browser: {exc}")
    else:
        print(f"Server health check did not become ready: {health_url}")


def existing_instance_healthy(host: str, port: int) -> bool:
    """Identify our edge service, not just any HTTP listener on this port."""
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as response:
            data = json.loads(response.read(65536))
            return isinstance(data, dict) and response.status == 200 and data.get('status') == 'ok' and 'store_id' in data and isinstance(data.get('runtime'), dict)
    except (OSError, ValueError, urllib.error.URLError):
        return False


def acquire_instance_lock(port: int) -> bool:
    """Windows kernel mutex survives startup races and is released on process exit."""
    global _INSTANCE_HANDLE
    if os.name != 'nt':
        return True
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    name = 'Global\\SnapKeyVisionAI' if port == 8091 else f'Global\\SnapKeyVisionAI-{port}'
    ctypes.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, name)
    if not handle:
        raise OSError(ctypes.get_last_error(), 'Could not create application instance lock')
    if ctypes.get_last_error() == 183:
        kernel.CloseHandle(handle)
        return False
    _INSTANCE_HANDLE = handle
    return True


def main() -> None:
    ensure_console_streams()
    parser = argparse.ArgumentParser(description="Madhushala Camera AI edge service")
    parser.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8091")))
    parser.add_argument("--background", action="store_true", help="Run without opening the setup browser.")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the setup browser.")
    parser.add_argument("--open-ui", action="store_true", help="Open the setup UI and exit.")
    args = parser.parse_args()

    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    host = args.host
    port = args.port
    log_level = os.environ.get("LOG_LEVEL", "info").lower()
    setup_url = f"http://{host}:{port}/setup"

    if args.open_ui:
        open_browser(setup_url)
        return

    foreground = env_bool("AUTO_OPEN_BROWSER", True) and not args.background and not args.no_browser
    if existing_instance_healthy(host, port):
        if foreground:
            open_browser(setup_url)
        return
    if not acquire_instance_lock(port):
        # The owner may still be importing models; do not race it with another server.
        if foreground:
            open_browser_when_ready(host, port)
        return
    # An older service started outside this launcher can become ready during locking.
    if existing_instance_healthy(host, port):
        if foreground:
            open_browser(setup_url)
        return

    from camera_service.api import app
    # The updater is deliberately started only for frozen Windows builds. It uses
    # the same scoped edge credential as normal cloud synchronization.
    try:
        from camera_service.api import cloud_client
        from camera_service.updater import EdgeUpdater
        updater = EdgeUpdater(cloud_client, interval_seconds=float(os.environ.get("CAMERA_UPDATE_INTERVAL_SECONDS", "1800")))
        updater.start()
    except Exception as exc:
        print(f"Updater startup skipped: {exc}")

    print(f"Starting SnapKey Vision AI on http://{host}:{port}")

    auto_open_browser = env_bool("AUTO_OPEN_BROWSER", True) and not args.background and not args.no_browser
    if auto_open_browser:
        browser_thread = threading.Thread(
            target=open_browser_when_ready,
            args=(host, port),
            daemon=True,
            name="BrowserOpenWhenReady",
        )
        browser_thread.start()

    uvicorn.run(
        app,
        host=host,
        port=port,
        log_level=log_level,
    )


ensure_console_streams()

if __name__ == "__main__":
    main()
