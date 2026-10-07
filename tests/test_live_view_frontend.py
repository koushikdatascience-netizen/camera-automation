from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_live_view_pages_pin_one_supported_sdk_and_load_shared_viewer_first():
    live = (ROOT / "cloud_portal/static/live.html").read_text(encoding="utf-8")
    attendance = (ROOT / "cloud_portal/static/attendance.html").read_text(encoding="utf-8")
    for page in (live, attendance):
        sdk = page.index("livekit-client@2.22.3")
        helper = page.index('/static/js/live-view.js')
        app = page.index('/static/js/main.js')
        assert sdk < helper < app
        assert page.count('/static/js/live-view.js') == 1


def test_shared_viewer_reconciles_remote_publications_and_plays_before_live():
    source = (ROOT / "cloud_portal/static/js/live-view.js").read_text(encoding="utf-8")
    assert 'publication?.isSubscribed?publication.track:null' in source
    assert 'p.identity.startsWith("edge-")' in source
    assert 'run.room.remoteParticipants.values()' in source
    assert 'track.attach(this.video)' in source
    assert 'this.stateFor(run,"playing")' in source
    assert 'TrackUnsubscribed' in source
    assert 'setSubscribed(true)' in source
    assert 'run.closed' in source
