"""The next-feed choice: state, expiry and the /api/feed routes."""

import pytest
from conftest import LAN_PEER, TOKEN

from openscan import feed
from openscan.web import dashboard_api
from openscan.web import server as srv

SCAN_TOKEN = "fedcba9876543210fedcba9876543210"


@pytest.fixture
def clock(monkeypatch):
    now = [5000.0]
    monkeypatch.setattr(feed, "_now", lambda: now[0])
    return now


def test_set_and_clear():
    assert feed.peek() is None
    feed.set_next("a")
    assert feed.next_profile_id() == "a"
    feed.set_next(None)
    assert feed.peek() is None
    feed.set_next("b")
    feed.clear()
    assert feed.next_profile_id() is None


def test_expires_after_ten_minutes(clock):
    feed.set_next("a")
    clock[0] += 599
    pid, left, _ = feed.peek()
    assert pid == "a" and left == 1
    clock[0] += 1
    assert feed.peek() is None
    assert feed.next_profile_id() is None


def test_countdown_counts_down(clock):
    feed.set_next("a")
    assert feed.peek()[1] == 600
    clock[0] += 100
    assert feed.peek()[1] == 500


def test_clear_if_only_clears_the_same_choice():
    feed.set_next("a")
    serial = feed.peek()[2]
    feed.set_next("b")
    feed.clear_if(serial)
    assert feed.next_profile_id() == "b"
    feed.clear_if(feed.peek()[2])
    assert feed.peek() is None


# --- routes --------------------------------------------------------------------

@pytest.fixture
def scan_server(live_server, app_config, fake_scanner):
    app_config.web.scan_token = SCAN_TOKEN
    fake_scanner.online = True
    live_server.set_peer(LAN_PEER)
    dashboard_api._scan_history.clear()
    yield live_server
    dashboard_api._scan_history.clear()


def _make_profile(s, name="Invoices"):
    r = s.post("/api/profiles", {"name": name, "destinations": [{"type": "folder", "options": {}}]},
               token=TOKEN)
    assert r.status == 201, r.body
    return r.json()["profile"]["id"]


def test_set_get_and_clear(scan_server, clock):
    s = scan_server
    pid = _make_profile(s)
    first = s.get("/api/feed", token=SCAN_TOKEN).json()
    assert first["next_profile"] is None and first["last_scans"] == []
    r = s.post("/api/feed/next", {"profile_id": pid}, token=SCAN_TOKEN)
    assert r.status == 200
    assert r.json()["next_profile"] == {"id": pid, "name": "Invoices"}
    clock[0] += 30
    got = s.get("/api/feed", token=SCAN_TOKEN).json()
    assert got["next_profile"]["id"] == pid and got["expires_in_seconds"] == 570
    assert got["adf_state"] == "Unknown" and got["scanner_state"] == "Idle"
    assert got["monitor_enabled"] is False
    cleared = s.post("/api/feed/next", {"profile_id": None}, token=SCAN_TOKEN)
    assert cleared.json()["next_profile"] is None
    assert s.get("/api/feed", token=SCAN_TOKEN).json()["next_profile"] is None


def test_expiry_shows_in_the_api(scan_server, clock):
    s = scan_server
    pid = _make_profile(s)
    s.post("/api/feed/next", {"profile_id": pid}, token=SCAN_TOKEN)
    clock[0] += feed.TTL_SECONDS
    got = s.get("/api/feed", token=SCAN_TOKEN).json()
    assert got["next_profile"] is None and got["expires_in_seconds"] == 0


def test_bad_requests(scan_server):
    s = scan_server
    assert s.post("/api/feed/next", {"profile_id": "nope"}, token=SCAN_TOKEN).status == 404
    assert s.post("/api/feed/next", {}, token=SCAN_TOKEN).status == 400
    assert feed.peek() is None


def test_deleted_profile_reads_as_cleared(scan_server):
    s = scan_server
    pid = _make_profile(s)
    other = _make_profile(s, "Other")
    s.post("/api/feed/next", {"profile_id": pid}, token=SCAN_TOKEN)
    assert s.post(f"/api/profiles/{pid}/delete", {}, token=TOKEN).status == 200
    assert s.get("/api/feed", token=SCAN_TOKEN).json()["next_profile"] is None
    assert other


def test_last_five_scans(scan_server):
    s = scan_server
    for i in range(7):
        dashboard_api.add_to_history({
            "filename": f"f{i}.pdf", "timestamp": f"2026-01-01T00:00:0{i}", "profile": "P",
            "ok": i != 6, "queued": False, "saved_paths": [], "errors": []})
    scans = s.get("/api/feed", token=SCAN_TOKEN).json()["last_scans"]
    assert [x["file"] for x in scans] == ["f6.pdf", "f5.pdf", "f4.pdf", "f3.pdf", "f2.pdf"]
    assert scans[0] == {"file": "f6.pdf", "time": "2026-01-01T00:00:06", "profile": "P",
                        "result": "failed"}
    assert scans[1]["result"] == "ok"


def test_feeder_state_shows(scan_server, fake_scanner):
    from openscan.scanner.base import AdfState, ScannerState, ScannerStatus
    fake_scanner.get_status = lambda info: ScannerStatus(state=ScannerState.IDLE,
                                                         adf_state=AdfState.LOADED)
    got = scan_server.get("/api/feed", token=SCAN_TOKEN).json()
    assert got["adf_state"] == "ScannerAdfLoaded" and got["scanner_state"] == "Idle"
    fake_scanner.online = False
    fake_scanner.get_status = type(fake_scanner).get_status.__get__(fake_scanner)
    assert scan_server.get("/api/feed", token=SCAN_TOKEN).json()["scanner_state"] == "Offline"


def test_scan_role_may_use_the_feed_routes(scan_server):
    assert srv.scan_role_allows("GET", "/api/feed")
    assert srv.scan_role_allows("POST", "/api/feed/next")
    assert not srv.scan_role_allows("POST", "/api/feed")
    assert not srv.scan_role_allows("GET", "/api/feed/next")
    assert scan_server.get("/api/feed").status == 401
    assert scan_server.post("/api/feed/next", {"profile_id": None}).status == 401
