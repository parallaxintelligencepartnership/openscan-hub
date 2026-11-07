"""SMB poll and email receiver: config, settings, services and status wiring."""

import copy
import json

from conftest import TOKEN

from openscan import services
from openscan.config import AppConfig, get_config_path, load_config, save_config


def test_config_round_trip_and_defaults(config_dir):
    cfg = load_config()
    assert cfg.smb_poll.enabled is False and cfg.smb_poll.after_delivery == "move"
    assert cfg.imap_receive.security == "ssl" and cfg.imap_receive.port == 993
    cfg.smb_poll.server = "nas"
    cfg.imap_receive.allowed_senders = ["@a.example", "b@c.example"]
    save_config(cfg)
    again = load_config()
    assert again.smb_poll.server == "nas"
    assert again.imap_receive.allowed_senders == ["@a.example", "b@c.example"]


def test_passwords_are_blanked_and_kept_when_blank(live_server, app_config):
    app_config.smb_poll.password = "smb-secret-pw"
    app_config.imap_receive.password = "imap-secret-pw"
    blob = json.dumps(live_server.get("/api/config", token=TOKEN).json())
    assert "smb-secret-pw" not in blob and "imap-secret-pw" not in blob
    data = json.loads(blob)
    assert data["smb_poll"]["has_password"] and data["imap_receive"]["has_password"]
    r = live_server.post("/api/settings", {
        "smb_poll": {"server": "nas", "password": ""},
        "imap_receive": {"host": "mail.example.com", "password": "", "allowed_senders": "a@b.example, @c.example"},
    }, token=TOKEN)
    assert r.status == 200, r.body
    from openscan.web import server as srv
    live = srv.get_config()
    assert live.smb_poll.password == "smb-secret-pw" and live.smb_poll.server == "nas"
    assert live.imap_receive.password == "imap-secret-pw"
    assert live.imap_receive.allowed_senders == ["a@b.example", "@c.example"]


def test_settings_reject_bad_choices(live_server, app_config):
    assert live_server.post("/api/settings", {"smb_poll": {"after_delivery": "burn"}}, token=TOKEN).status == 400
    assert live_server.post("/api/settings", {"imap_receive": {"security": "none"}}, token=TOKEN).status == 400
    assert live_server.post("/api/settings", {"imap_receive": {"port": 0}}, token=TOKEN).status == 400


def test_services_start_stop_with_the_settings(fake_services):
    old = AppConfig()
    old.wizard_completed = True
    new = copy.deepcopy(old)
    new.smb_poll.enabled = True
    new.imap_receive.enabled = True
    report = services.apply_config(old, new)
    assert "SMB share poll" in report["started"] and "email receiver" in report["started"]
    changed = copy.deepcopy(new)
    changed.imap_receive.host = "other.example"
    assert services.apply_config(new, changed)["restarted"] == ["email receiver"]
    off = copy.deepcopy(changed)
    off.smb_poll.enabled = False
    assert services.apply_config(changed, off)["stopped"] == ["SMB share poll"]


def test_status_lists_the_new_receivers(live_server, app_config):
    app_config.smb_poll.server = "nas"
    app_config.smb_poll.share = "scans"
    app_config.imap_receive.host = "mail.example.com"
    rx = live_server.get("/api/status", token=TOKEN).json()["receivers"]
    assert rx["smb"]["enabled"] is False and rx["smb"]["server"] == "nas"
    assert rx["email"]["host"] == "mail.example.com"
    assert "password" not in json.dumps(rx)
