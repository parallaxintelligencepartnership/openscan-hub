"""Settings and wizard saves (review finding I4)."""

import json

from conftest import LAN_PEER, TOKEN

from openscan.config import get_config_path, load_config
from openscan.web import server as srv


def _saved() -> dict:
    return json.loads(get_config_path().read_text())


def test_config_is_redacted(live_server, app_config):
    app_config.paperless.api_token = "paperless-secret"
    data = live_server.get("/api/config", token=TOKEN).json()
    blob = json.dumps(data)
    assert "paperless-secret" not in blob
    assert TOKEN not in blob
    assert "ftp-secret-pw" not in blob
    assert data["paperless"]["has_token"] is True
    assert data["ftp_receive"]["has_password"] is True


def test_settings_round_trip_keeps_secrets_when_blank(live_server, app_config):
    app_config.paperless.api_token = "paperless-secret"
    r = live_server.post("/api/settings", {
        "output": {"folder": app_config.output.folder, "filename_pattern": "doc_{n}"},
        "paperless": {"enabled": True, "mode": "api", "api_url": "http://pl.local:8000",
                      "api_token": "", "tags": "inbox, scanned"},
        "ftp_receive": {"password": "", "port": 2121},
        "scan": {"resolution": 200, "color_mode": "gray"},
        "web": {"api_token": "attacker-chosen"},
    }, token=TOKEN)
    assert r.status == 200, r.body
    live = srv.get_config()
    assert live.paperless.api_token == "paperless-secret"
    assert live.ftp_receive.password == "ftp-secret-pw"
    assert live.web.api_token == TOKEN  # not settable through settings
    assert live.paperless.tags == ["inbox", "scanned"]
    assert live.output.filename_pattern == "doc_{n}"
    assert live.scan.resolution == 200 and live.scan.color_mode == "gray"
    on_disk = _saved()
    assert on_disk["paperless"]["api_token"] == "paperless-secret"
    assert on_disk["paperless"]["api_url"] == "http://pl.local:8000"

    # A non-blank secret replaces the old one.
    live_server.post("/api/settings", {"paperless": {"api_token": "new-one"}}, token=TOKEN)
    assert srv.get_config().paperless.api_token == "new-one"


def test_settings_rejects_bad_values(live_server):
    r = live_server.post("/api/settings", {"scan": {"color_mode": "rainbow"}}, token=TOKEN)
    assert r.status == 400
    r = live_server.post("/api/settings", {"paperless": {"api_url": "ftp://x"}}, token=TOKEN)
    assert r.status == 400
    r = live_server.post("/api/settings", {"web": {"port": 70000}}, token=TOKEN)
    assert r.status == 400


def test_bind_change_reports_restart(live_server, fake_services):
    from openscan import services
    services.set_running_web("127.0.0.1", live_server.port)
    srv.get_config().web.port = live_server.port
    r = live_server.post("/api/settings", {"web": {"bind": "0.0.0.0"}}, token=TOKEN).json()
    assert r["restart_needed"] is True
    assert live_server.get("/api/config", token=TOKEN).json()["restart_needed"] is True


def test_settings_start_and_stop_services_live(live_server, fake_services, tmp_path):
    watch = tmp_path / "inbox"
    r = live_server.post("/api/settings", {"folder_watch": {"enabled": True, "watch_folder": str(watch)}},
                         token=TOKEN)
    assert r.status == 200
    assert "folder_watch" in fake_services.names("start")
    live_server.post("/api/settings", {"folder_watch": {"enabled": False}}, token=TOKEN)
    assert "folder_watch" in fake_services.names("stop")


def test_wizard_save_merges_instead_of_wiping(live_server, app_config, tmp_path):
    app_config.wizard_completed = False
    app_config.paperless.api_token = "paperless-secret"
    app_config.paperless.api_url = "http://pl.local:8000"
    app_config.homeassistant.enabled = True
    app_config.folder_watch.enabled = True
    app_config.folder_watch.watch_folder = str(tmp_path / "old-inbox")
    out = tmp_path / "new" / "Scans"
    body = {
        "scanner": {"ip": "192.0.2.20", "port": 80, "protocol": "escl", "name": "MFP", "model": "MFP 1", "path": ""},
        "output": {"folder": str(out)},
        "monitor": {"enabled": False},
        "ftp_receive": {"enabled": False},
        "folder_watch": {"enabled": False},
        "paperless": {"enabled": False},
    }
    r = live_server.post("/api/save-config", body, token=TOKEN)
    assert r.status == 200, r.body
    live = srv.get_config()
    assert live.wizard_completed is True
    assert live.scanner.ip == "192.0.2.20"
    assert out.is_dir()  # created on save
    assert live.paperless.api_token == "paperless-secret"
    assert live.paperless.api_url == "http://pl.local:8000"
    assert live.homeassistant.enabled is True
    assert live.folder_watch.enabled is False  # explicitly off now
    assert live.web.api_token == TOKEN
    assert live.ftp_receive.password == "ftp-secret-pw"
    reloaded = load_config(get_config_path())
    assert reloaded.paperless.api_token == "paperless-secret"


def test_wizard_save_needs_a_folder(live_server):
    r = live_server.post("/api/save-config", {"output": {"folder": ""}}, token=TOKEN)
    assert r.status == 400


def test_regenerate_token(live_server):
    r = live_server.post("/api/token/regenerate", {}, token=TOKEN)
    assert r.status == 200
    new = r.json()["api_token"]
    assert new != TOKEN and len(new) == 32
    cookie = r.cookie()
    live_server.set_peer(LAN_PEER)
    assert live_server.get("/api/config", token=TOKEN).status == 401
    assert live_server.get("/api/config", token=new).status == 200
    assert live_server.get("/api/config", cookie=cookie).status == 200


def test_secrets_need_auth(live_server):
    live_server.set_peer(LAN_PEER)
    assert live_server.get("/api/secrets").status == 401
    data = live_server.get("/api/secrets", token=TOKEN).json()
    assert data == {"api_token": TOKEN, "ftp_password": "ftp-secret-pw"}


def test_errors_endpoint_lists_recent_problems(live_server):
    import logging
    from openscan.logging_setup import clear_recent_errors, setup_logging
    setup_logging(level="INFO")
    clear_recent_errors()
    logging.getLogger("openscan.test").warning("Paperless upload failed: timeout")
    data = live_server.get("/api/errors", token=TOKEN).json()
    assert data["errors"][0]["message"] == "Paperless upload failed: timeout"
    assert data["errors"][0]["level"] == "WARNING"


def test_queue_retry(live_server, monkeypatch):
    import sys
    import types

    calls = []
    queue = types.SimpleNamespace(
        pending=lambda: [{"id": "a1", "file": "/x/scan.pdf", "destination": "paperless",
                          "attempts": 2, "last_error": "down", "next_try": ""}],
        retry_now=lambda i: calls.append(i) or True,
        retry_all=lambda: 3,
    )
    fake = types.ModuleType("openscan.retry_queue")
    fake.get_queue = lambda: queue
    monkeypatch.setitem(sys.modules, "openscan.retry_queue", fake)
    import openscan
    monkeypatch.setattr(openscan, "retry_queue", fake, raising=False)

    assert live_server.get("/api/queue", token=TOKEN).json()["pending"][0]["id"] == "a1"
    assert live_server.post("/api/queue/retry", {"id": "a1"}, token=TOKEN).json() == {"retried": 1}
    assert calls == ["a1"]
    assert live_server.post("/api/queue/retry", {}, token=TOKEN).json() == {"retried": 3}
    assert live_server.get("/api/health").json()["queue_pending"] == 1


def test_service_that_cannot_start_is_a_save_warning(live_server, app_config, fake_services):
    mod = fake_services.module("openscan.scanner.ftp_receive")
    mod.start_ftp_receiver = lambda cfg: False
    mod.ftp_receiver_error = lambda: "port 2121 is in use by another program"
    msg = "FTP receiver could not start: port 2121 is in use by another program"
    r = live_server.post("/api/settings", {"ftp_receive": {"enabled": True}}, token=TOKEN)
    assert r.status == 200, r.body
    assert msg in r.json()["warnings"]
    r = live_server.post("/api/settings", {"ftp_receive": {"enabled": False}}, token=TOKEN)
    r = live_server.post("/api/save-config", {
        "output": {"folder": app_config.output.folder},
        "ftp_receive": {"enabled": True},
    }, token=TOKEN)
    assert r.status == 200, r.body
    assert msg in r.json()["warnings"]


def test_defaults_show_the_ftp_address(live_server, app_config):
    app_config.ftp_receive.passive_address = "192.168.1.9"
    data = live_server.get("/api/defaults", token=TOKEN).json()
    assert data["ftp_address"] == "192.168.1.9"
