"""Scanning, status and the Home Assistant module (review I1 UI half, I3, I5, A19)."""

import threading
import time
from dataclasses import dataclass, field

import pytest
from conftest import LAN_PEER, TOKEN

from openscan.scanner.base import ScanSource, release_scan, try_acquire_scan
from openscan.web import dashboard_api


@dataclass
class DeliveryResult:  # same shape lane C returns
    ok: bool
    saved_paths: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    queued: bool = False


@pytest.fixture
def save_result(monkeypatch):
    holder = {"value": DeliveryResult(ok=True, saved_paths=["/scans/a.pdf"])}
    monkeypatch.setattr(dashboard_api, "save_scan_for_config", lambda cfg, pdf, name: holder["value"])
    return holder


def _hold_lock_in_thread():
    """Hold the scan lock from another thread (as the ADF monitor would)."""
    taken, release = threading.Event(), threading.Event()

    def holder():
        assert try_acquire_scan(timeout=0)
        taken.set()
        release.wait(5)
        release_scan()

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    taken.wait(2)
    return release, t


def test_scan_busy_returns_409(live_server, save_result):
    release, t = _hold_lock_in_thread()
    try:
        r = live_server.post("/api/scan", {"source": "flatbed"}, token=TOKEN)
        assert r.status == 409
        assert r.json()["error"] == "Scanner is busy"
        r = live_server.post("/api/test-scan", {"ip": "192.0.2.10", "protocol": "escl"}, token=TOKEN)
        assert r.status == 409
    finally:
        release.set()
        t.join(5)
    assert live_server.post("/api/scan", {"source": "flatbed"}, token=TOKEN).status == 200


def test_status_answers_while_a_scan_runs(live_server, fake_scanner, save_result):
    fake_scanner.gate = threading.Event()
    results = {}
    t = threading.Thread(target=lambda: results.update(
        r=live_server.post("/api/scan", {"source": "adf"}, token=TOKEN)), daemon=True)
    t.start()
    time.sleep(0.2)
    start = time.monotonic()
    status = live_server.get("/api/status", token=TOKEN)
    assert status.status == 200
    assert time.monotonic() - start < 2
    assert status.json()["scan_in_progress"] is True
    fake_scanner.gate.set()
    t.join(5)
    assert results["r"].status == 200
    assert fake_scanner.scans[0].source == ScanSource.ADF


def test_scan_success_and_failure_are_reported_honestly(live_server, save_result):
    ok = live_server.post("/api/scan", {"source": "flatbed"}, token=TOKEN).json()
    assert ok["success"] is True and ok["ok"] is True
    assert ok["saved_paths"] == ["/scans/a.pdf"]

    save_result["value"] = DeliveryResult(ok=False, errors=["NAS offline"], queued=True)
    queued = live_server.post("/api/scan", {"source": "flatbed"}, token=TOKEN).json()
    assert queued["success"] is False and queued["queued"] is True
    assert "retried" in queued["message"]

    save_result["value"] = DeliveryResult(ok=False, errors=["disk full"])
    failed = live_server.post("/api/scan", {"source": "flatbed"}, token=TOKEN).json()
    assert failed["success"] is False and failed["queued"] is False
    assert "disk full" in failed["message"]

    history = live_server.get("/api/history", token=TOKEN).json()["scans"]
    assert [h["ok"] for h in history[:3]] == [False, False, True]


def test_old_dict_shape_is_adapted():
    assert dashboard_api.delivery_summary({"saved": [], "errors": [{"type": "folder", "error": "x"}]})["ok"] is False
    assert dashboard_api.delivery_summary({"saved": [{"type": "folder", "path": "/a"}], "errors": []}) == {
        "ok": True, "queued": False, "saved_paths": ["/a"], "errors": []}
    assert dashboard_api.delivery_summary({"saved": [], "errors": []})["ok"] is False


def test_status_includes_sources_for_feeder_button(live_server, fake_scanner):
    fake_scanner.online = True
    fake_scanner.sources = [ScanSource.FLATBED, ScanSource.ADF]
    data = live_server.get("/api/status", token=TOKEN).json()
    assert data["mode"] == "scanner"
    assert data["sources"] == ["Platen", "Feeder"]
    assert data["scanner"]["sources"] == ["Platen", "Feeder"]


def test_status_offline_has_no_sources(live_server, fake_scanner):
    data = live_server.get("/api/status", token=TOKEN).json()
    assert data["status"]["state"] == "Offline"
    assert data["sources"] == []


def test_ftp_mode_status_has_no_scanner(live_server, app_config):
    app_config.scanner.ip = ""
    app_config.scanner.protocol = "ftp"
    app_config.ftp_receive.enabled = True
    data = live_server.get("/api/status", token=TOKEN).json()
    assert data["mode"] == "ftp"
    assert "scanner" not in data
    assert data["receivers"]["ftp"]["port"] == 2121
    r = live_server.post("/api/scan", {"source": "flatbed"}, token=TOKEN)
    assert r.status == 400


def test_ha_routes_absent_when_disabled(live_server):
    live_server.set_peer(LAN_PEER)
    assert live_server.get(f"/api/ha/health?token={TOKEN}").status == 404
    assert live_server.get(f"/api/ha/scan?source=adf&token={TOKEN}").status == 404


def test_ha_routes_need_token(live_server, app_config, fake_scanner, save_result):
    app_config.homeassistant.enabled = True
    live_server.set_peer(LAN_PEER)
    assert live_server.get("/api/ha/health").status == 401
    assert live_server.get("/api/ha/scan?source=adf").status == 401
    # A browser session cookie is not enough for module routes.
    live_server.set_peer(None)
    cookie = live_server.get("/dashboard").cookie()
    assert live_server.get("/api/ha/health", cookie=cookie).status == 401

    live_server.set_peer(LAN_PEER)
    h = live_server.get(f"/api/ha/health?token={TOKEN}")
    assert h.status == 200 and h.json()["status"] == "ok"
    r = live_server.request("POST", "/api/ha/scan?source=adf", token=TOKEN)
    assert r.status == 202 and r.json()["source"] == "adf"
    for _ in range(50):
        if fake_scanner.scans:
            break
        time.sleep(0.05)
    assert fake_scanner.scans[0].source == ScanSource.ADF


def test_ha_scan_busy_is_409(live_server, app_config):
    app_config.homeassistant.enabled = True
    release, t = _hold_lock_in_thread()
    try:
        r = live_server.get(f"/api/ha/scan?source=flatbed&token={TOKEN}")
        assert r.status == 409
    finally:
        release.set()
        t.join(5)


def test_discover_timeout_is_clamped(live_server, monkeypatch):
    seen = []
    from openscan.web import wizard_api
    monkeypatch.setattr(wizard_api, "discover_all", lambda timeout: seen.append(timeout) or [])
    live_server.post("/api/discover", {"timeout": 100000}, token=TOKEN)
    live_server.post("/api/discover", {"timeout": 0}, token=TOKEN)
    assert seen == [15.0, 1.0]
