"""Profile API, profile-driven scan settings, the monitor and Home Assistant."""

import json
import time

import pytest
from conftest import LAN_PEER, TOKEN

from openscan import monitor, profiles
from openscan.config import AppConfig, get_config_path
from openscan.profiles import Profile
from openscan.scanner.base import ScanSource
from openscan.web import server as srv


def _saved() -> dict:
    return json.loads(get_config_path().read_text())


def _new(live_server, tmp_path, **extra):
    body = {"name": "Invoices", "icon": "R", "source": "adf", "color_mode": "gray",
            "resolution": 600, "duplex": True, "page_size": "a4",
            "processing": {"remove_blank_pages": True, "compression": "small",
                           "ocr_languages": "eng, deu"},
            "destinations": [{"type": "folder", "options": {"folder": str(tmp_path / "inv")}}],
            "name_rule": "inv_{n}", "split": "none", "show_on_home": True, **extra}
    return live_server.post("/api/profiles", body, token=TOKEN)


def test_list_has_the_default_profile(live_server):
    data = live_server.get("/api/profiles", token=TOKEN).json()
    assert data["default_profile_id"] == "default"
    [p] = data["profiles"]
    assert p["name"] == "Default" and p["follow_settings"] is True and p["is_default"]


def test_routes_need_sign_in(live_server):
    live_server.set_peer(LAN_PEER)
    assert live_server.get("/api/profiles").status == 401
    assert live_server.post("/api/profiles", {"name": "x"}).status == 401
    assert live_server.post("/api/profiles/default/scan", {}).status == 401
    assert live_server.get("/api/destinations").status == 401


def test_create_get_and_saved(live_server, tmp_path):
    r = _new(live_server, tmp_path)
    assert r.status == 201, r.body
    p = r.json()["profile"]
    assert p["id"].startswith("invoices-") and p["follow_settings"] is False
    assert p["processing"]["compression"] == "small"
    assert p["processing"]["ocr_languages"] == ["eng", "deu"]
    got = live_server.get(f"/api/profiles/{p['id']}", token=TOKEN).json()["profile"]
    assert got["resolution"] == 600 and got["destinations"][0]["type"] == "folder"
    on_disk = _saved()
    assert [x["name"] for x in on_disk["profiles"]] == ["Default", "Invoices"]
    assert live_server.get("/api/profiles/nope", token=TOKEN).status == 404


@pytest.mark.parametrize("change", [
    {"name": ""}, {"name": 5}, {"color_mode": "rainbow"}, {"resolution": 5},
    {"resolution": True}, {"source": "tray"}, {"split": "magic"}, {"duplex": "yes"},
    {"processing": {"compression": "huge"}}, {"processing": {"ocr": "on"}},
    {"processing": {"ocr_languages": ["../x"]}}, {"destinations": []},
    {"destinations": [{"type": "ftp-of-doom", "options": {}}]},
    {"destinations": [{"type": "folder", "options": {"folder": 3}}]},
    {"name_rule": ""}, {"name": "Default"},
])
def test_create_rejects_bad_input(live_server, tmp_path, change):
    r = _new(live_server, tmp_path, **change)
    assert r.status == 400, change
    assert len(live_server.get("/api/profiles", token=TOKEN).json()["profiles"]) == 1


def test_secrets_are_blanked_and_kept(live_server, tmp_path):
    dest = {"type": "paperless_api", "options": {"url": "http://pl.local:8000",
                                                 "token": "pl-secret", "tags": "a, b"}}
    p = _new(live_server, tmp_path, destinations=[dest]).json()["profile"]
    assert "pl-secret" not in json.dumps(live_server.get("/api/profiles", token=TOKEN).json())
    opts = p["destinations"][0]["options"]
    assert opts["token"] == "" and opts["has_token"] is True and opts["tags"] == ["a", "b"]
    # A blank token in an update keeps the stored one; has_token is dropped.
    r = live_server.post(f"/api/profiles/{p['id']}", {"destinations": p["destinations"],
                                                      "name": "Bills"}, token=TOKEN)
    assert r.status == 200, r.body
    stored = profiles.find(srv.get_config(), p["id"])
    assert stored.name == "Bills"
    assert stored.destinations[0]["options"]["token"] == "pl-secret"
    assert "has_token" not in stored.destinations[0]["options"]
    # A new token replaces it.
    dest["options"]["token"] = "new-secret"
    live_server.post(f"/api/profiles/{p['id']}", {"destinations": [dest]}, token=TOKEN)
    assert profiles.find(srv.get_config(), p["id"]).destinations[0]["options"]["token"] == \
        "new-secret"


def test_editing_default_stops_following_settings(live_server, app_config):
    r = live_server.post("/api/profiles/default", {"color_mode": "bw"}, token=TOKEN)
    assert r.status == 200, r.body
    p = r.json()["profile"]
    assert p["follow_settings"] is False and p["color_mode"] == "bw"
    # The other values were taken from Settings when it was edited.
    assert p["destinations"] == [{"type": "folder", "options": {"folder": app_config.output.folder}}]
    assert _saved()["profiles"][0]["follow_settings"] is False


def test_reorder_default_delete(live_server, tmp_path):
    a = _new(live_server, tmp_path, name="A").json()["profile"]["id"]
    b = _new(live_server, tmp_path, name="B").json()["profile"]["id"]
    r = live_server.post("/api/profiles/reorder", {"ids": [b, "default", a]}, token=TOKEN)
    assert [p["id"] for p in r.json()["profiles"]] == [b, "default", a]
    assert live_server.post("/api/profiles/reorder", {"ids": [b, a]}, token=TOKEN).status == 400
    r = live_server.post(f"/api/profiles/{a}/default", {}, token=TOKEN)
    assert r.json()["default_profile_id"] == a
    r = live_server.post(f"/api/profiles/{a}/delete", {}, token=TOKEN)
    assert r.json()["default_profile_id"] == b
    live_server.post(f"/api/profiles/{b}/delete", {}, token=TOKEN)
    r = live_server.post("/api/profiles/default/delete", {}, token=TOKEN)
    assert r.status == 400  # the last profile stays
    assert live_server.post("/api/profiles/default/explode", {}, token=TOKEN).status == 404


def test_destinations_and_test_button(live_server, tmp_path):
    types = {d["type"]: d for d in live_server.get("/api/destinations", token=TOKEN).json()
             ["destinations"]}
    assert types["folder"]["fields"][0]["key"] == "folder"
    assert "paperless_api" in types
    r = live_server.post("/api/destinations/test",
                         {"type": "folder", "options": {"folder": str(tmp_path / "t")}}, token=TOKEN)
    assert r.status == 200 and r.json()["ok"] is True, r.body
    r = live_server.post("/api/destinations/test", {"type": "nope"}, token=TOKEN)
    assert r.status == 400


def test_name_preview_uses_smart_fields(live_server):
    live_server.post("/api/naming/vendors", {"vendors": ["Acme Corp", "acme corp", " "]},
                     token=TOKEN)
    assert live_server.get("/api/naming/vendors", token=TOKEN).json()["vendors"] == ["Acme Corp"]
    assert _saved()["naming"]["vendors"] == ["Acme Corp"]
    r = live_server.post("/api/profiles/preview", {
        "name_rule": "{vendor}_{doc_date}_{title|none}",
        "text": "Letter from ACME CORP\nRECEIPT\nPaid March 3, 2026"}, token=TOKEN)
    assert r.json()["filename"] == "Acme Corp_2026-03-03_RECEIPT.pdf"
    r = live_server.post("/api/profiles/preview", {"name_rule": "{vendor|no-vendor}"}, token=TOKEN)
    assert r.json()["filename"] == "no-vendor.pdf"


def test_profile_scan_uses_profile_settings(live_server, fake_scanner, tmp_path):
    fake_scanner.online = True
    pid = _new(live_server, tmp_path).json()["profile"]["id"]
    r = live_server.post(f"/api/profiles/{pid}/scan", {}, token=TOKEN)
    assert r.status == 200, r.body
    data = r.json()
    assert data["success"] is True and data["filename"] == "inv_001.pdf"
    s = fake_scanner.scans[-1]
    assert (s.source, s.color_mode, s.resolution, s.duplex, s.page_size) == \
        (ScanSource.ADF, "gray", 600, True, "a4")
    assert (tmp_path / "inv" / "inv_001.pdf").read_bytes() == fake_scanner.data
    assert live_server.post("/api/profiles/missing/scan", {}, token=TOKEN).status == 404


def test_default_profile_follows_settings_page(live_server, fake_scanner, app_config):
    app_config.scan.color_mode, app_config.scan.resolution = "bw", 150
    app_config.scan.duplex, app_config.scan.page_size = True, "legal"
    assert live_server.post("/api/scan", {"source": "adf"}, token=TOKEN).status == 200
    s = fake_scanner.scans[-1]
    assert (s.color_mode, s.resolution, s.duplex, s.page_size) == ("bw", 150, True, "legal")
    live_server.post("/api/scan", {"source": "flatbed"}, token=TOKEN)
    assert fake_scanner.scans[-1].duplex is False  # duplex is for the feeder only


def test_ha_scan_takes_a_profile(live_server, app_config, fake_scanner, tmp_path):
    app_config.homeassistant.enabled = True
    _new(live_server, tmp_path, name="Receipts", source="flatbed", color_mode="bw")
    live_server.set_peer(LAN_PEER)
    r = live_server.get(f"/api/ha/scan?profile=receipts&source=adf&token={TOKEN}")
    assert r.status == 202 and r.json()["source"] == "flatbed", r.body
    for _ in range(50):
        if fake_scanner.scans:
            break
        time.sleep(0.05)
    assert fake_scanner.scans[0].color_mode == "bw"
    r = live_server.request("POST", "/api/ha/scan", token=TOKEN,
                            json_body={"profile": "nobody"})
    assert r.status == 404


def test_monitor_scans_with_the_default_profile():
    cfg = AppConfig()
    cfg.profiles = [profiles.from_settings(cfg),
                    Profile(id="fast", name="Fast", color_mode="gray", resolution=150,
                            duplex=True, page_size="letter")]
    cfg.default_profile_id = "fast"
    seen = []

    class Impl:
        def scan(self, info, settings):
            seen.append(settings)
            return b"%PDF-"

    assert monitor._auto_scan_pages(cfg, None, Impl()) == b"%PDF-"
    s = seen[0]
    assert (s.source, s.color_mode, s.resolution, s.duplex, s.page_size) == \
        (ScanSource.ADF, "gray", 150, True, "letter")


def test_profile_scan_says_who_started_it(live_server, fake_scanner, monkeypatch):
    from openscan.web import dashboard_api
    seen = {}
    real = dashboard_api.intake.handle_scan
    monkeypatch.setattr(dashboard_api.intake, "handle_scan",
                        lambda cfg, data, **kw: seen.update(kw) or real(cfg, data, **kw))
    live_server.set_peer(LAN_PEER)
    assert live_server.post("/api/profiles/default/scan", {"source": "flatbed"},
                            token=TOKEN).status == 200
    assert seen["started_by"] == f"web admin {LAN_PEER}"
    assert seen["profile"].id == "default"


def test_profiles_page_and_dashboard_block(live_server):
    page = live_server.get("/profiles", token=TOKEN)
    assert page.status == 200 and b"js/profiles.js" in page.body
    dash = live_server.get("/dashboard", token=TOKEN).body
    assert b'id="profile-buttons"' in dash and b"js/profiles.js" in dash
    assert b'href="profiles"' in live_server.get("/settings", token=TOKEN).body
