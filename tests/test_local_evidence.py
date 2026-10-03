from datetime import datetime, timezone
from fastapi.testclient import TestClient


def test_persisted_clip_availability_safe_urls_and_range(tmp_path, monkeypatch):
    import camera_service.api as api
    from camera_service.storage import SQLiteStore
    store = SQLiteStore(str(tmp_path / 'edge.db'))
    monkeypatch.setattr(api, 'store', store)
    root = tmp_path / 'evidence'; root.mkdir()
    clip = root / 'unknown.mp4'; clip.write_bytes(b'0123456789')
    photo = root / 'photo.jpg'; photo.write_bytes(b'photo')
    now = datetime.now(timezone.utc)
    incident, _ = store.upsert_unknown('shop', 'camera', 'track', now, now, now, 2, .1, str(photo))
    client = TestClient(api.app)
    before = client.get('/api/v1/unknown-incidents').json()['items'][0]
    assert before['snapshot_available'] and not before['clip_available']
    store.update_unknown_clip(incident, str(clip))
    item = client.get('/api/v1/unknown-incidents').json()['items'][0]
    assert item['clip_available'] and item['clip_path'] is True
    assert str(tmp_path) not in str(item)
    response = client.get(item['clip_url'], headers={'Range': 'bytes=2-5'})
    assert response.status_code == 206 and response.content == b'2345'
    clip.unlink()
    assert not client.get('/api/v1/unknown-incidents').json()['items'][0]['clip_available']
    outside = tmp_path / 'private.mp4'; outside.write_bytes(b'private')
    alert = store.create_security_alert('shop', 'camera', 'SECURITY', 'object', .9, now, clip_path=str(outside))
    assert not client.get('/api/v1/security-alerts').json()['items'][0]['clip_available']
    assert client.get(f"/api/v1/security-alerts/{alert['id']}/clip").status_code == 404
