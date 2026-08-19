#!/usr/bin/env python3
"""
Test script for camera management functionality
"""

import os
import tempfile
import shutil
from camera_service.camera_manager import CameraManager

def test_camera_manager():
    """Test the camera manager functionality"""

    # Create a temporary database
    with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as tmp_db:
        db_path = tmp_db.name

    try:
        # Initialize camera manager
        manager = CameraManager(db_path)

        # Test 1: Create a camera
        print("Test 1: Creating camera...")
        camera_data = {
            'camera_id': 'test_camera_01',
            'name': 'Test Camera',
            'rtsp_url': 'rtsp://admin:password@192.168.1.100:554/Streaming/Channels/102',
            'enabled': True,
            'camera_role': 'ENTRANCE_EXIT',
            'features': {
                'attendance': True,
                'face_recognition': True,
                'unknown_detection': True,
                'shoplifting': False
            }
        }

        camera = manager.create_camera(camera_data)
        print(f"✓ Camera created: {camera.camera_id}")

        # Test 2: List cameras
        print("\nTest 2: Listing cameras...")
        cameras = manager.list_cameras()
        print(f"✓ Found {len(cameras)} cameras")
        assert len(cameras) == 1
        assert cameras[0].camera_id == 'test_camera_01'

        # Test 3: Get camera by ID
        print("\nTest 3: Getting camera by ID...")
        retrieved_camera = manager.get_camera('test_camera_01')
        print(f"✓ Retrieved camera: {retrieved_camera.name}")
        assert retrieved_camera.name == 'Test Camera'

        # Test 4: Update camera
        print("\nTest 4: Updating camera...")
        updates = {
            'name': 'Updated Test Camera',
            'enabled': False
        }
        updated_camera = manager.update_camera('test_camera_01', updates)
        print(f"✓ Camera updated: {updated_camera.name}, Enabled: {updated_camera.enabled}")
        assert updated_camera.name == 'Updated Test Camera'
        assert updated_camera.enabled == False

        # Test 5: Test RTSP password masking
        print("\nTest 5: Testing RTSP password masking...")
        masked_url = manager._mask_rtsp_password('rtsp://admin:secret123@192.168.1.100:554/Streaming/Channels/102')
        print(f"Original: rtsp://admin:secret123@192.168.1.100:554/Streaming/Channels/102")
        print(f"Masked:   {masked_url}")
        assert '*****' in masked_url
        assert 'secret123' not in masked_url

        # Test 6: Test RTSP connection (with invalid URL - should fail gracefully)
        print("\nTest 6: Testing RTSP connection...")
        result = manager.test_rtsp_connection('rtsp://invalid:url@192.168.1.255:554/nonexistent')
        print(f"✓ RTSP test completed with success={result['success']}, message='{result['message']}'")
        assert result['success'] == False

        # Test 7: Delete camera
        print("\nTest 7: Deleting camera...")
        success = manager.delete_camera('test_camera_01')
        print(f"✓ Camera deleted: {success}")
        assert success == True

        # Verify deletion
        cameras_after_delete = manager.list_cameras()
        print(f"✓ Cameras after deletion: {len(cameras_after_delete)}")
        assert len(cameras_after_delete) == 0

        print("\n🎉 All camera management tests passed!")

    finally:
        # Clean up
        if os.path.exists(db_path):
            os.unlink(db_path)

if __name__ == '__main__':
    test_camera_manager()