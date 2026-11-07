"""Profiles: migration from old settings, JSON round trip, lookup, redaction."""

import json

from openscan import _edition, profiles
from openscan.config import AppConfig, load_config, save_config
from openscan.processing import ProcessOptions
from openscan.profiles import Profile

OLD_CONFIG = {
    "wizard_completed": True,
    "output": {"folder": "/srv/scans", "filename_pattern": "doc_{date}_{n}"},
    "scan": {"resolution": 200, "color_mode": "gray", "page_size": "a4", "duplex": True,
             "remove_blank_pages": True},
    "paperless": {"enabled": True, "mode": "api", "api_url": "http://pl:8000",
                  "api_token": "tok-123", "tags": ["Inbox"], "correspondent": "Bank",
                  "keep_local_copy": False},
    "web": {"api_token": "0" * 32},
    "ftp_receive": {"password": "pw-not-default"},
}


def _write(tmp_path, data):
    p = tmp_path / "openscan.json"
    p.write_text(json.dumps(data))
    return p


def test_old_config_gets_a_default_profile(tmp_path):
    p = _write(tmp_path, OLD_CONFIG)
    cfg = load_config(p)
    [default] = cfg.profiles
    assert cfg.default_profile_id == default.id == "default"
    assert default.name == "Default" and default.follow_settings is True
    assert (default.color_mode, default.resolution, default.page_size, default.duplex) == \
        ("gray", 200, "a4", True)
    assert default.processing.remove_blank_pages is True
    assert default.name_rule == "doc_{date}_{n}"
    assert [d["type"] for d in default.destinations] == ["folder", "paperless_api"]
    assert default.destinations[1]["options"]["correspondent"] == "Bank"
    # Old sections stay readable and unchanged; the token is not copied.
    saved = json.loads(p.read_text())
    assert saved["paperless"]["api_token"] == "tok-123"
    assert saved["output"]["filename_pattern"] == "doc_{date}_{n}"
    assert "tok-123" not in json.dumps(saved["profiles"])


def test_round_trip_keeps_profiles(tmp_path):
    p = tmp_path / "openscan.json"
    cfg = AppConfig()
    cfg.profiles = [Profile(id="inv", name="Invoices", icon="receipt", source="adf",
                            processing=ProcessOptions(ocr=True, ocr_languages=["eng", "deu"],
                                                      compression="small"),
                            destinations=[{"type": "folder", "options": {"folder": "/x"}}],
                            name_rule="{profile}_{date}", split="blank", show_on_home=False)]
    cfg.default_profile_id = "inv"
    save_config(cfg, p)
    again = load_config(p)
    assert again.profiles == cfg.profiles and again.default_profile_id == "inv"
    save_config(again, p)
    assert load_config(p).profiles == cfg.profiles


def test_bad_profile_values_are_skipped(tmp_path):
    data = dict(OLD_CONFIG, profiles=[
        {"id": "a", "name": "A", "source": "tray", "resolution": "high", "split": "barcode",
         "destinations": [{"type": "folder"}, {"options": {}}, "junk"]},
        {"id": "a", "name": "duplicate"},
        "junk",
    ], default_profile_id="missing")
    cfg = load_config(_write(tmp_path, data))
    [a] = cfg.profiles
    assert a.source == "auto" and a.resolution == 300 and a.split == "barcode"
    assert a.destinations == [{"type": "folder", "options": {}}]
    assert cfg.default_profile_id == "a"


def test_follow_settings_profile_tracks_later_settings_changes():
    cfg = AppConfig()
    cfg.output.folder = "/a"
    profiles.migrate(cfg)
    cfg.output.folder = "/b"
    cfg.scan.remove_blank_pages = True
    live = profiles.get(cfg)
    assert live.destinations == [{"type": "folder", "options": {"folder": "/b"}}]
    assert live.processing.remove_blank_pages is True
    cfg.profiles[0].follow_settings = False
    assert profiles.get(cfg).destinations == [{"type": "folder", "options": {"folder": "/a"}}]


def test_get_without_profiles_and_unknown_id():
    cfg = AppConfig()
    cfg.output.folder = "/scans"
    assert profiles.get(cfg).destinations[0]["options"]["folder"] == "/scans"
    cfg.profiles = [Profile(id="one", name="One"), Profile(id="two", name="Two")]
    cfg.default_profile_id = "two"
    assert profiles.get(cfg).id == "two"
    assert profiles.get(cfg, "one").id == "one"
    assert profiles.get(cfg, "nope").id == "two"


def test_office_ignores_paperless_settings(monkeypatch):
    monkeypatch.setattr(_edition, "EDITION", "office")
    cfg = AppConfig()
    cfg.output.folder = "/scans"
    cfg.paperless.enabled, cfg.paperless.mode, cfg.paperless.consume_folder = True, "consume", "/c"
    assert [d["type"] for d in profiles.get(cfg).destinations] == ["folder"]
    assert cfg.paperless.consume_folder == "/c"


def test_audit_default_follows_edition(monkeypatch):
    assert AppConfig().audit.enabled is False
    monkeypatch.setattr(_edition, "EDITION", "office")
    assert AppConfig().audit.enabled is True


def test_redacted_config_hides_profile_secrets():
    from openscan.web.settings_api import redacted_config
    cfg = AppConfig()
    cfg.profiles = [Profile(id="p", name="P", destinations=[
        {"type": "paperless_api", "options": {"url": "http://pl", "token": "s3cret"}}])]
    data = redacted_config(cfg)
    opts = data["profiles"][0]["destinations"][0]["options"]
    assert opts["token"] == "" and opts["has_token"] is True and opts["url"] == "http://pl"
    assert cfg.profiles[0].destinations[0]["options"]["token"] == "s3cret"
