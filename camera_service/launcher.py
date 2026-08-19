import os
import sys
import time
import webbrowser
import threading
import subprocess
import socket
from pathlib import Path

def check_port(host, port, timeout=10):
    """Check if a port is available"""
    start_time = time.time()
    while time.time() - start_time < timeout:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except (socket.timeout, ConnectionRefusedError):
            time.sleep(0.1)
    return False

def open_browser(url):
    """Open browser after a delay to ensure server is ready"""
    time.sleep(2)  # Wait a bit for the server to start
    try:
        webbrowser.open(url)
    except Exception as e:
        print(f"Failed to open browser: {e}")

def main():
    # Set up environment
    os.environ.setdefault('PYTHONUNBUFFERED', '1')

    # Get the directory of this script
    script_dir = Path(__file__).parent

    # Start the FastAPI application
    print("Starting Camera Automation...")

    # Use uvicorn to run the application
    cmd = [
        sys.executable, '-m', 'uvicorn',
        'camera_service.api:app',
        '--host', '127.0.0.1',
        '--port', '8000'
    ]

    # Add reload flag only in debug mode
    if os.environ.get('DEBUG', 'false').lower() == 'true':
        cmd.append('--reload')

    # Start the server process
    process = subprocess.Popen(cmd, cwd=script_dir.parent)

    # Check if server started successfully
    if check_port('127.0.0.1', 8000, timeout=30):
        print("Server started successfully on http://127.0.0.1:8000")

        # Open browser if not in debug mode
        if os.environ.get('AUTO_OPEN_BROWSER', 'true').lower() != 'false':
            browser_thread = threading.Thread(target=open_browser, args=('http://127.0.0.1:8000/setup',))
            browser_thread.daemon = True
            browser_thread.start()
    else:
        print("Failed to start server")
        process.terminate()
        sys.exit(1)

    # Wait for the process to complete
    try:
        process.wait()
    except KeyboardInterrupt:
        print("Shutting down...")
        process.terminate()
        process.wait()

if __name__ == '__main__':
    main()