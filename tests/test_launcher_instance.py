import io
import json
import sys
import pytest
from camera_service import launcher


@pytest.mark.parametrize('payload,healthy', [({'status':'ok','store_id':'s','runtime':{}}, True),
                                           ({'status':'ok'}, False), ({'status':'error','store_id':'s','runtime':{}}, False)])
def test_existing_service_identity(monkeypatch, payload, healthy):
    class Response(io.BytesIO):
        status = 200
    monkeypatch.setattr(launcher.urllib.request, 'urlopen', lambda *a, **k: Response(json.dumps(payload).encode()))
    assert launcher.existing_instance_healthy('127.0.0.1', 8091) is healthy


@pytest.mark.parametrize('background', [False, True])
def test_healthy_second_launch_never_runs_server(monkeypatch, background):
    monkeypatch.setattr(sys, 'argv', ['launcher'] + (['--background'] if background else []))
    monkeypatch.setattr(launcher, 'existing_instance_healthy', lambda *a: True)
    monkeypatch.setattr(launcher, 'acquire_instance_lock', lambda *a: pytest.fail('healthy service needs no new lock'))
    monkeypatch.setattr(launcher.uvicorn, 'run', lambda *a, **k: pytest.fail('must not start second server'))
    opened = []
    monkeypatch.setattr(launcher, 'open_browser', opened.append)
    monkeypatch.setenv('AUTO_OPEN_BROWSER', '1')
    launcher.main()
    assert bool(opened) is (not background)


def test_starting_instance_never_runs_second_server(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['launcher', '--background'])
    monkeypatch.setattr(launcher, 'existing_instance_healthy', lambda *a: False)
    monkeypatch.setattr(launcher, 'acquire_instance_lock', lambda *a: False)
    monkeypatch.setattr(launcher.uvicorn, 'run', lambda *a, **k: pytest.fail('must not race startup'))
    launcher.main()
