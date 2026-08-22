#!/usr/bin/env python3
"""
Comprehensive test script for the production package functionality
"""

import os
import sys
import tempfile
import json
import time
import threading
import subprocess
import requests
from pathlib import Path

def test_api_endpoints():
    """Test all the new API endpoints"""
    print("[TEST] Testing API Endpoints...")

    # Start the application in a separate process
    print("Starting test server...")
    process = subprocess.Popen([
        sys.executable, '-m', 'uvicorn',
        'camera_service.api:app',
        '--host', '127.0.0.1',
        '--port', '8001'
    ], cwd=os.getcwd())

    # Poll /health until it responds or timeout
    base_url = "http://127.0.0.1:8001"
    health_timeout = 120  # seconds
    health_poll_interval = 0.5
    start_time = time.time()
    
    print("Waiting for /health endpoint to become available...")
    while time.time() - start_time < health_timeout:
        try:
            response = requests.get(f"{base_url}/health", timeout=2)
            if response.status_code == 200:
                data = response.json()
                if data.get('status') == 'ok':
                    print("[OK] Health endpoint working")
                    break
        except requests.exceptions.RequestException:
            pass
        time.sleep(health_poll_interval)
    else:
        # If we get here, health check timed out
        process.terminate()
        process.wait()
        raise TimeoutError(f"/health endpoint did not become available within {health_timeout} seconds")

    # Poll /ready until it reports ready or timeout
    ready_timeout = 180  # seconds
    ready_poll_interval = 1.0
    start_time = time.time()
    
    print("Waiting for /ready endpoint to report ready...")
    while time.time() - start_time < ready_timeout:
        try:
            response = requests.get(f"{base_url}/ready", timeout=2)
            if response.status_code == 200:
                data = response.json()
                # Check for 'status' field which is what the endpoint actually returns
                if data.get('status') == 'ready':
                    print("[OK] Ready endpoint reports ready")
                    break
            elif response.status_code == 503:
                # Still initializing, continue polling
                pass
        except requests.exceptions.RequestException:
            pass
        time.sleep(ready_poll_interval)
    else:
        # If we get here, ready check timed out
        process.terminate()
        process.wait()
        raise TimeoutError(f"/ready endpoint did not report ready within {ready_timeout} seconds")

    try:
        # Test 1: Health endpoint
        print("\n1. Testing /health endpoint...")
        response = requests.get(f"{base_url}/health")
        assert response.status_code == 200
        data = response.json()
        assert data['status'] == 'ok'
        print("[OK] Health endpoint working")

        # Test 2: Setup UI endpoint
        print("\n2. Testing /setup endpoint...")
        response = requests.get(f"{base_url}/setup")
        assert response.status_code == 200
        assert "Camera Automation Setup" in response.text
        print("[OK] Setup UI endpoint working")

        # Test 3: Camera CRUD endpoints
        print("\n3. Testing Camera CRUD endpoints...")
        requests.delete(f"{base_url}/api/v1/cameras/test_camera_01")

        # Create camera
        camera_data = {
            "camera_id": "test_camera_01",
            "name": "Test Camera",
            "source_type": "rtsp",
            "rtsp_url": "rtsp://admin:password@192.168.1.100:554/Streaming/Channels/102",
            "enabled": True,
            "camera_role": "ENTRANCE_EXIT",
            "features": {
                "attendance": True,
                "face_recognition": True,
                "unknown_detection": True,
                "shoplifting": False
            }
        }

        response = requests.post(f"{base_url}/api/v1/cameras", json=camera_data)
        assert response.status_code == 200
        created_camera = response.json()['camera']
        assert created_camera['camera_id'] == "test_camera_01"
        assert "*****" in created_camera['rtsp_url']  # Password should be masked
        print("[OK] Camera creation working with password masking")

        # List cameras
        response = requests.get(f"{base_url}/api/v1/cameras")
        assert response.status_code == 200
        cameras = response.json()['items']
        test_camera = next((camera for camera in cameras if camera['camera_id'] == "test_camera_01"), None)
        assert test_camera is not None
        print("[OK] Camera listing working")

        # Get single camera
        response = requests.get(f"{base_url}/api/v1/cameras/test_camera_01")
        assert response.status_code == 200
        camera = response.json()
        assert camera['camera_id'] == "test_camera_01"
        print("[OK] Single camera retrieval working")

        # Update camera
        update_data = {
            "name": "Updated Test Camera",
            "enabled": False
        }
        response = requests.patch(f"{base_url}/api/v1/cameras/test_camera_01", json=update_data)
        assert response.status_code == 200
        updated_camera = response.json()
        assert updated_camera['name'] == "Updated Test Camera"
        assert updated_camera['enabled'] == False
        print("[OK] Camera update working")

        # Test 4: RTSP connection test
        print("\n4. Testing RTSP connection endpoint...")
        test_data = {
            "rtsp_url": "rtsp://invalid:url@192.168.1.255:554/nonexistent"
        }
        response = requests.post(f"{base_url}/api/v1/cameras/test", json=test_data)
        assert response.status_code == 200
        result = response.json()
        assert result['success'] == False
        assert 'message' in result
        print("[OK] RTSP connection test working (fails gracefully with invalid URL)")

        # Test 5: Camera status endpoint
        print("\n5. Testing camera status endpoint...")
        response = requests.get(f"{base_url}/api/v1/cameras/test_camera_01/status")
        # This might return 404 if status not set, which is expected for this test
        if response.status_code == 404:
            print("[OK] Camera status endpoint working (returns 404 for non-existent status)")
        else:
            assert response.status_code == 200
            print("[OK] Camera status endpoint working")

        # Test 6: Camera control endpoints
        print("\n6. Testing camera control endpoints...")
        response = requests.post(f"{base_url}/api/v1/cameras/test_camera_01/start")
        assert response.status_code == 200
        assert response.json()['status'] in {'online', 'degraded'}
        print("[OK] Camera start endpoint working")

        response = requests.post(f"{base_url}/api/v1/cameras/test_camera_01/stop")
        assert response.status_code == 200
        assert response.json()['status'] == 'stopping'
        print("[OK] Camera stop endpoint working")

        response = requests.post(f"{base_url}/api/v1/cameras/test_camera_01/restart")
        assert response.status_code == 200
        assert response.json()['status'] in {'restarted_online', 'restarted_degraded'}
        print("[OK] Camera restart endpoint working")

        # Test 7: Delete camera
        print("\n7. Testing camera deletion...")
        response = requests.delete(f"{base_url}/api/v1/cameras/test_camera_01")
        assert response.status_code == 200
        assert response.json()['deleted'] == True
        print("[OK] Camera deletion working")

        # Test 8: Personnel endpoints (existing functionality)
        print("\n8. Testing personnel endpoints...")
        personnel_data = {
            "employee_code": f"TEST{int(time.time()) % 10000:04d}",
            "full_name": "Test User",
            "role": "WORKER"
        }
        response = requests.post(f"{base_url}/api/v1/personnel", json=personnel_data)
        assert response.status_code == 200
        person = response.json()
        assert person['employee_code'] == personnel_data['employee_code']
        print("[OK] Personnel creation working")

        # Test 9: Attendance endpoints (existing functionality)
        print("\n9. Testing attendance endpoints...")
        response = requests.get(f"{base_url}/api/v1/attendance")
        assert response.status_code == 200
        assert 'items' in response.json()
        print("[OK] Attendance endpoints working")

        # Test 10: Unknown incidents endpoints (existing functionality)
        print("\n10. Testing unknown incidents endpoints...")
        response = requests.get(f"{base_url}/api/v1/unknown-incidents")
        assert response.status_code == 200
        assert 'items' in response.json()
        print("[OK] Unknown incidents endpoints working")

        print("\n[DONE] All API endpoint tests passed!")

    except Exception as e:
        print(f"[ERROR] API test failed: {e}")
        raise
    finally:
        # Clean up
        process.terminate()
        process.wait()

def test_camera_manager_directly():
    """Test the camera manager directly"""
    print("\n[TEST] Testing Camera Manager Directly...")

    # Create a temporary database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        db_path = tmp_db.name

    try:
        from camera_service.camera_manager import CameraManager

        manager = CameraManager(db_path)

        # Test password masking
        test_url = "rtsp://admin:secret123@192.168.1.100:554/Streaming/Channels/102"
        masked_url = manager._mask_rtsp_password(test_url)
        assert "*****" in masked_url
        assert "secret123" not in masked_url
        print("[OK] Password masking working")

        # Test RTSP connection (should fail gracefully)
        result = manager.test_rtsp_connection("rtsp://invalid:url@192.168.1.255:554/nonexistent")
        assert result['success'] == False
        assert 'message' in result
        print("[OK] RTSP connection test working")

        print("[OK] Camera manager direct tests passed!")

    finally:
        if os.path.exists(db_path):
            os.unlink(db_path)

def test_launcher_script():
    """Test the launcher script functionality"""
    print("\n[TEST] Testing Launcher Script...")

    # Test that the launcher script exists and is valid Python
    launcher_path = "camera_service/launcher.py"
    assert os.path.exists(launcher_path)

    with open(launcher_path, 'r') as f:
        launcher_code = f.read()
        assert 'check_port' in launcher_code
        assert 'open_browser' in launcher_code
        assert 'uvicorn' in launcher_code
        assert 'main' in launcher_code

    print("[OK] Launcher script structure validated!")

def test_packaging_files():
    """Test that all packaging files exist"""
    print("\n[TEST] Testing Packaging Files...")

    required_files = [
        "packaging/windows/build_windows.ps1",
        "packaging/windows/README.md",
        "camera_service/web/setup.html",
        "camera_service/web/favicon.ico",
        "camera_service/web/static",
        "START_CAMERA_AUTOMATION.bat",
        "STOP_CAMERA_AUTOMATION.bat",
        ".env.example"
    ]

    for file_path in required_files:
        if not os.path.exists(file_path):
            print(f"[ERROR] Missing file: {file_path}")
            return False

    print("[OK] All packaging files present!")

    # Test build script content
    with open("packaging/windows/build_windows.ps1", 'r') as f:
        build_script = f.read()
        assert 'PyInstaller' in build_script
        assert 'CameraAutomation' in build_script
        assert 'pyinstaller' in build_script

    print("[OK] Build script content validated!")

    return True

def main():
    """Run all tests"""
    print("[START] Starting Camera Automation Production Package Tests\n")

    try:
        # Test 1: Packaging files
        if not test_packaging_files():
            print("[ERROR] Packaging files test failed")
            return False

        # Test 2: Camera manager directly
        test_camera_manager_directly()

        # Test 3: Launcher script
        test_launcher_script()

        # Test 4: API endpoints (this will start a test server)
        test_api_endpoints()

        print("\n[DONE] ALL TESTS PASSED! Production package is ready!")

        return True

    except Exception as e:
        print(f"\n[ERROR] Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == '__main__':
    success = main()
    sys.exit(0 if success else 1)
