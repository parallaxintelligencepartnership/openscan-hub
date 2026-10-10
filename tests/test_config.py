import json
import os
import stat
from dataclasses import asdict

import pytest

from openscan.config import AppConfig, load_config, save_config


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in ("OPENSCAN_BIND", "OPENSCAN_PORT", "OPENSCAN_TOKEN"):
        monkeypatch.delenv(k, raising=False)


def test_round_trip(tmp_path):
    p = tmp_path / "c.json"
    cfg = AppConfig()
    cfg.web.port = 9000
    cfg.web.api_token = "a" * 32
    cfg.ftp_receive.password = "secretpw"
    cfg.paperless.tags = ["x", "y"]
    cfg.scan.color_mode = "gray"
    save_config(cfg, p)
    loaded = load_config(p)
    assert asdict(loaded) == asdict(cfg)


def test_web_port_not_a_field_and_alias():
    cfg = AppConfig()
    assert "web_port" not in asdict(cfg)
    cfg.web_port = 1234
    assert cfg.web.port == 1234
    assert cfg.web_port == 1234


def test_migrations(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"web_port": 8123, "paperless": {"default_tags": ["a", "b"]}}))
    cfg = load_config(p)
    assert cfg.web.port == 8123
    assert cfg.paperless.tags == ["a", "b"]


def test_generated_secrets_persisted(tmp_path):
    p = tmp_path / "c.json"
    cfg = load_config(p)
    assert len(cfg.web.api_token) == 32
    assert len(cfg.ftp_receive.password) == 16
    again = load_config(p)
    assert again.web.api_token == cfg.web.api_token
    assert again.ftp_receive.password == cfg.ftp_receive.password
    if os.name == "posix":
        assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_old_default_ftp_password_replaced(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"ftp_receive": {"password": "scan"}}))
    assert load_config(p).ftp_receive.password != "scan"


def test_malformed_json_returns_defaults(tmp_path):
    p = tmp_path / "c.json"
    p.write_text("{not json")
    cfg = load_config(p)
    assert cfg.web.port == 8020
    assert cfg.web.api_token


def test_unknown_keys_and_bad_types(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({
        "bogus": 1,
        "web": {"port": "oops", "nope": True},
        "scanner": "notadict",
        "monitor": {"poll_interval": [1]},
    }))
    cfg = load_config(p)
    assert cfg.web.port == 8020
    assert cfg.monitor.poll_interval == 3.0


def test_env_overrides_not_persisted(tmp_path, monkeypatch):
    p = tmp_path / "c.json"
    monkeypatch.setenv("OPENSCAN_BIND", "0.0.0.0")
    monkeypatch.setenv("OPENSCAN_PORT", "9999")
    monkeypatch.setenv("OPENSCAN_TOKEN", "envtoken")
    cfg = load_config(p)
    assert (cfg.web.bind, cfg.web.port, cfg.web.api_token) == ("0.0.0.0", 9999, "envtoken")
    saved = json.loads(p.read_text())
    assert saved["web"]["bind"] == "127.0.0.1"
    assert saved["web"]["port"] == 8020
    assert saved["web"]["api_token"] != "envtoken"
