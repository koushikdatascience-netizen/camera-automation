import os
import threading
import time
import urllib.error
import urllib.request
import webbrowser

import uvicorn

from camera_service.api import app


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


def main() -> None:
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    log_level = os.environ.get("LOG_LEVEL", "info").lower()

    print(f"Starting Camera Automation on http://{host}:{port}")

    if env_bool("AUTO_OPEN_BROWSER", True):
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


if __name__ == "__main__":
    main()
