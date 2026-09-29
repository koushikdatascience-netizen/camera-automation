from camera_service.camera.onvif import _with_credentials, select_profile


def test_onvif_rtsp_credentials_are_encoded_safely():
    uri=_with_credentials("rtsp://192.168.1.20:554/Streaming/Channels/101","admin","p@ss:word")
    assert uri.startswith("rtsp://admin:p%40ss%3Aword@192.168.1.20:554/")
    assert "p@ss:word" not in uri


def test_ai_profile_prefers_lower_bandwidth_stream():
    profiles=[
        {"token":"main","uri":"rtsp://cam/main","width":1920,"height":1080,"fps":25},
        {"token":"sub","uri":"rtsp://cam/sub","width":640,"height":360,"fps":15},
    ]
    assert select_profile(profiles,"ai")["token"]=="sub"
    assert select_profile(profiles,"recording")["token"]=="main"


def test_profile_selection_ignores_profiles_without_stream_uri():
    profiles=[
        {"token":"broken","uri":"","width":320,"height":180,"fps":10},
        {"token":"valid","uri":"rtsp://cam/live","width":1280,"height":720,"fps":20},
    ]
    assert select_profile(profiles,"ai")["token"]=="valid"
