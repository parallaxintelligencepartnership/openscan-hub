"""Cross-lane wiring: dashboard and monitor deliver through save_scan_for_config,
history entries share one shape, multipage uses scan_page and blank removal."""

import json
import os
import time
from io import BytesIO

import pytest
from conftest import TOKEN
from PIL import Image, ImageDraw
from pypdf import PdfReader

from openscan import monitor, multipage, output, pages
from openscan.config import AppConfig
from openscan.scanner.base import AdfState, ScannerState, ScannerStatus
from openscan.web import dashboard_api


def _pdf(blank=False) -> bytes:
    img = Image.new("RGB", (600, 800), "white")
    if not blank:
        ImageDraw.Draw(img).rectangle((100, 100, 500, 700), fill="black")
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return pages.ensure_pdf(buf.getvalue())


@pytest.fixture(autouse=True)
def _clean():
    with dashboard_api._history_lock:
        dashboard_api._scan_history.clear()
    multipage._sessions.clear()
    yield
    with dashboard_api._history_lock:
        dashboard_api._scan_history.clear()
    multipage._sessions.clear()


def test_dashboard_scan_saves_real_pdf_and_reports_saved_name(live_server, fake_scanner, app_config,
                                                              monkeypatch):
    fake_scanner.data = _pdf()
    folder = app_config.output.folder
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, "scan_test.pdf"), "wb") as f:
        f.write(b"existing")
    monkeypatch.setattr(dashboard_api, "generate_filename", lambda pattern, *a, **k: "scan_test.pdf")

    r = live_server.post("/api/scan", {"source": "flatbed"}, token=TOKEN)
    assert r.status == 200
    data = r.json()
    assert data["success"] is True and data["ok"] is True and data["queued"] is False
    saved = data["saved_paths"][0]
    assert os.path.basename(saved) == data["filename"] != "scan_test.pdf"
    assert data["results"] == {"ok": True, "saved_paths": [saved], "errors": [], "queued": False}
    assert data["size_bytes"] == len(fake_scanner.data)
    assert len(PdfReader(saved).pages) == 1
    assert fake_scanner.scans[0].color_mode == app_config.scan.color_mode

    history = live_server.get("/api/history", token=TOKEN).json()["scans"]
    assert history[0]["filename"] == data["filename"]
    assert history[0]["ok"] is True and history[0]["saved_paths"] == [saved]


def test_dashboard_delivery_uses_every_paperless_option(app_config, monkeypatch):
    seen = {}
    monkeypatch.setattr(output, "save_scan",
                        lambda **kw: seen.update(kw) or output.DeliveryResult(ok=True, saved_paths=["/x/a.pdf"]))
    p = app_config.paperless
    p.enabled, p.mode, p.api_url, p.api_token = True, "api", "http://paperless.invalid", "t"
    p.correspondent, p.document_type, p.keep_local_copy = "Bank", "Statement", False
    summary = dashboard_api.save_document(app_config, b"%PDF", "a.pdf")
    assert summary["ok"] is True
    assert seen["paperless_correspondent"] == "Bank"
    assert seen["paperless_document_type"] == "Statement"
    assert seen["keep_local_copy"] is False


def test_multipage_save_removes_blank_pages_when_asked(live_server, fake_scanner, app_config):
    app_config.scan.remove_blank_pages = True
    sid = live_server.post("/api/multipage/start", {"source": "flatbed"}, token=TOKEN).json()["session_id"]
    for blank in (False, True):
        fake_scanner.data = _pdf(blank)
        r = live_server.post("/api/multipage/scan", {"session_id": sid}, token=TOKEN)
        assert r.status == 200 and r.json()["success"] is True
    data = live_server.post("/api/multipage/save", {"session_id": sid}, token=TOKEN).json()
    assert data["success"] is True
    assert len(PdfReader(data["saved_paths"][0]).pages) == 1


class _FeederProtocol:
    def __init__(self):
        self.adf = AdfState.EMPTY
        self.polls = 0

    def get_status(self, info):
        self.polls += 1
        return ScannerStatus(state=ScannerState.IDLE, adf_state=self.adf)

    def scan(self, info, settings):
        self.adf = AdfState.EMPTY
        return b"%PDF-1.4 feeder"


def test_monitor_failed_delivery_shows_as_not_saved_in_history(monkeypatch, tmp_path):
    fake = _FeederProtocol()
    monkeypatch.setattr(monitor, "_make_impl", lambda proto: fake)
    monkeypatch.setattr(monitor, "save_scan_for_config",
                        lambda cfg, pdf, name: output.DeliveryResult(ok=False, errors=["NAS offline"]))
    cfg = AppConfig()
    cfg.scanner.protocol, cfg.scanner.ip = "escl", "127.0.0.1"
    cfg.output.folder = str(tmp_path)
    cfg.monitor.enabled, cfg.monitor.poll_interval, cfg.monitor.settle_time = True, 0.02, 0.01
    try:
        monitor.start_monitor(cfg)
        end = time.monotonic() + 3
        while fake.polls < 2 and time.monotonic() < end:
            time.sleep(0.01)
        fake.adf = AdfState.LOADED
        while not dashboard_api._scan_history and time.monotonic() < end:
            time.sleep(0.01)
    finally:
        monitor.stop_monitor()
    entry = dashboard_api._scan_history[0]
    assert entry["auto"] is True and entry["source"] == "Feeder"
    assert entry["ok"] is False and entry["errors"] == ["NAS offline"]
    json.dumps(entry)


def test_history_entries_from_receivers_get_the_flat_shape():
    dashboard_api.add_to_history({"filename": "f.pdf", "source": "ftp", "auto": True, "success": False,
                                  "results": {"ok": False, "saved_paths": [], "errors": ["x"], "queued": True}})
    entry = dashboard_api._scan_history[0]
    assert entry["ok"] is False and entry["queued"] is True and entry["errors"] == ["x"]
