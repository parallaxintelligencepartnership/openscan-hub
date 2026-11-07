"""Build edition: current(), has(), the --set command, /api/edition and the HA gate."""

import pytest
from conftest import TOKEN

from openscan import _edition, edition


@pytest.fixture
def office(monkeypatch):
    monkeypatch.setattr(_edition, "EDITION", "office")


def test_repository_edition_is_homelab():
    assert edition.current() == "homelab"
    assert edition.has("homeassistant") and edition.has("paperless")
    assert not edition.has("audit_default_on")
    assert edition.has("scan_role")
    assert not edition.has("no_such_feature")


def test_office_features(office):
    assert edition.current() == "office"
    assert edition.features() == {"homeassistant": False, "paperless": False,
                                  "audit_default_on": True, "scan_role": True}
    assert edition.product_name() == "OpenScanHub Office"


def test_unknown_edition_value_means_homelab(monkeypatch):
    monkeypatch.setattr(_edition, "EDITION", "enterprise")
    assert edition.current() == "homelab"


def test_set_rewrites_the_edition_file(tmp_path):
    path = tmp_path / "_edition.py"
    edition.set_edition("office", path)
    ns = {}
    exec(path.read_text(), ns)
    assert ns["EDITION"] == "office"
    with pytest.raises(ValueError):
        edition.set_edition("pro", path)


def test_cli_set_writes_the_package_file(tmp_path, monkeypatch, capsys):
    target = tmp_path / "_edition.py"
    monkeypatch.setattr(edition, "edition_file", lambda: target)
    assert edition.main(["--set", "office"]) == 0
    assert 'EDITION = "office"' in target.read_text()
    assert edition.main([]) == 0  # prints the running edition
    assert capsys.readouterr().out.strip().endswith("homelab")


def test_api_edition(live_server):
    data = live_server.get("/api/edition", token=TOKEN).json()
    assert data == {"edition": "homelab", "features": edition.features()}
    assert live_server.get("/api/edition").status == 401


def test_office_does_not_register_home_assistant(office, monkeypatch):
    from openscan.integrations import homeassistant
    from openscan.web import server
    calls = []
    monkeypatch.setattr(homeassistant, "register", lambda: calls.append(1))
    server.load_routes()
    assert calls == []
    monkeypatch.setattr(_edition, "EDITION", "homelab")
    server.load_routes()
    assert calls == [1]
