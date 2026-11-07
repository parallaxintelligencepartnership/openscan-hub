"""Office edition: the build tree, the hidden controls and the refused settings."""

import http.client
import json
import os
import subprocess
import sys
import importlib.util
from pathlib import Path

import pytest
from conftest import TOKEN

ROOT = Path(__file__).resolve().parent.parent


def _load_build_exe():
    spec = importlib.util.spec_from_file_location("build_exe_under_test", ROOT / "build_exe.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def office_tree(tmp_path):
    return _load_build_exe().make_edition_tree(ROOT, "office", tmp_path / "src")


def test_office_tree_has_no_home_or_paperless_code(office_tree):
    assert (office_tree / "openscan" / "_edition.py").read_text().count('EDITION = "office"') == 1
    assert not (office_tree / "openscan" / "integrations").exists()
    assert not (office_tree / "openscan" / "destinations" / "paperless.py").exists()
    assert not list(office_tree.rglob("paperless.py"))
    assert not list(office_tree.rglob("homeassistant.py"))
    assert (office_tree / "openscan" / "destinations" / "folder.py").exists()


def test_building_a_tree_leaves_the_working_tree_alone(office_tree):
    assert 'EDITION = "homelab"' in (ROOT / "openscan" / "_edition.py").read_text()
    assert (ROOT / "openscan" / "integrations").is_dir()
    assert (ROOT / "openscan" / "destinations" / "paperless.py").is_file()


def test_homelab_tree_keeps_everything(tmp_path):
    tree = _load_build_exe().make_edition_tree(ROOT, "homelab", tmp_path / "src")
    assert (tree / "openscan" / "integrations" / "homeassistant.py").is_file()
    assert (tree / "openscan" / "destinations" / "paperless.py").is_file()


def test_exe_names():
    build = _load_build_exe()
    assert build.exe_name("homelab") == "OpenScanHub"
    assert build.exe_name("office") == "OpenScanHub-Office"


_START = r"""
import http.client, json, sys, threading
import openscan
from pathlib import Path
from openscan import edition
from openscan.config import AppConfig
from openscan.web import server as srv

assert Path(openscan.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve()), openscan.__file__
assert edition.current() == "office"
# An editable install resolves submodules from the real source tree; drop it
# so the test sees only the built tree.
sys.meta_path[:] = [f for f in sys.meta_path if "Editable" not in type(f).__name__ and "editable" not in getattr(f, "__module__", "")]
for name in ("openscan.integrations", "openscan.destinations.paperless"):
    try:
        __import__(name)
    except ImportError:
        pass
    else:
        raise SystemExit("module present: " + name)
from openscan import destinations
types = [c.type for c in destinations.available()]
assert "folder" in types and "paperless_api" not in types and "paperless_consume" not in types, types

cfg = AppConfig()
cfg.web.port = 0
cfg.web.api_token = "t" * 32
cfg.web.advertise_mdns = False
server = srv.create_server(cfg)
threading.Thread(target=server.serve_forever, daemon=True).start()
conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
conn.request("GET", "/api/edition", headers={"X-OpenScan-Token": "t" * 32})
resp = conn.getresponse()
print(resp.status, json.dumps(json.loads(resp.read())))
server.shutdown()
"""


def test_office_tree_imports_and_serves(office_tree, tmp_path):
    env = dict(os.environ, PYTHONPATH=str(office_tree), OPENSCAN_CONFIG_DIR=str(tmp_path / "cfg"))
    env.pop("OPENSCAN_TOKEN", None)
    (tmp_path / "cfg").mkdir()
    out = subprocess.run([sys.executable, "-c", _START, str(office_tree)], cwd=office_tree, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    status, body = out.stdout.strip().splitlines()[-1].split(" ", 1)
    data = json.loads(body)
    assert status == "200"
    assert data["edition"] == "office"
    assert data["features"]["homeassistant"] is False and data["features"]["paperless"] is False
    assert data["features"]["scan_role"] is True


_LEAK = r"""
import http.client, sys, threading
sys.meta_path[:] = [f for f in sys.meta_path if "Editable" not in type(f).__name__ and "editable" not in getattr(f, "__module__", "")]
from openscan import edition
assert edition.current() == "office"
from openscan.config import AppConfig
from openscan.profiles import Profile
from openscan.web import server as srv
cfg = AppConfig()
cfg.web.port = 0; cfg.web.api_token = "a" * 32; cfg.web.scan_token = "s" * 32
cfg.web.advertise_mdns = False
cfg.profiles = [Profile(id="p", name="Docs", follow_settings=False, destinations=[
    {"type": "folder", "options": {"folder": "/tmp/x"}},
    {"type": "paperless_api", "options": {"url": "http://paperless.example",
                                          "token": "PAPERLESS-SECRET-123"}}])]
cfg.default_profile_id = "p"
server = srv.create_server(cfg)
threading.Thread(target=server.serve_forever, daemon=True).start()
for path, tok in (("/api/profiles", "s" * 32), ("/api/profiles", "a" * 32),
                  ("/api/profiles/p", "a" * 32), ("/api/config", "a" * 32)):
    c = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    c.request("GET", path, headers={"X-OpenScan-Token": tok, "X-Forwarded-For": "10.0.0.9"})
    r = c.getresponse()
    body = r.read().decode()
    print(path, tok[0], r.status, "PAPERLESS-SECRET-123" in body, "paperless.example" in body)
server.shutdown()
"""


def test_office_blanks_secrets_of_paperless_destinations_it_does_not_ship(office_tree, tmp_path):
    # A Homelab config moved to the Office build keeps its paperless_api
    # destination (ignored). Its token must not show in any answer.
    env = dict(os.environ, PYTHONPATH=str(office_tree), OPENSCAN_CONFIG_DIR=str(tmp_path / "cfg"))
    env.pop("OPENSCAN_TOKEN", None)
    (tmp_path / "cfg").mkdir()
    out = subprocess.run([sys.executable, "-c", _LEAK], cwd=office_tree, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr[-2000:]
    rows = [line.split() for line in out.stdout.strip().splitlines()]
    assert [r[:3] for r in rows] == [["/api/profiles", "s", "200"], ["/api/profiles", "a", "200"],
                                     ["/api/profiles/p", "a", "200"], ["/api/config", "a", "200"]]
    assert all(r[3] == "False" for r in rows), rows  # the token never shows
    assert rows[0][4] == "False"  # the scan role gets no options at all


def test_dockerfile_has_the_edition_arg_and_strip_step():
    text = (ROOT / "Dockerfile").read_text()
    assert "ARG EDITION=homelab" in text
    assert "python -m openscan.edition --set office" in text
    assert "openscan/integrations" in text and "openscan/destinations/paperless.py" in text


@pytest.fixture
def office(monkeypatch):
    from openscan import edition
    monkeypatch.setattr(edition, "current", lambda: "office")


def test_office_reports_its_edition(office, live_server):
    data = live_server.get("/api/edition", token=TOKEN).json()
    assert data["edition"] == "office" and data["role"] == "admin"
    assert data["features"]["paperless"] is False


@pytest.mark.parametrize("path", ["/api/settings", "/api/save-config"])
@pytest.mark.parametrize("body", [
    {"paperless": {"enabled": True, "api_url": "http://x"}},
    {"homeassistant": {"enabled": True}},
])
def test_office_refuses_to_save_home_assistant_and_paperless(office, live_server, app_config, path, body):
    body = dict(body, output={"folder": app_config.output.folder})
    r = live_server.post(path, body, token=TOKEN)
    assert r.status == 400
    assert "not part of this edition" in r.json()["error"]
    cfg = live_server.get("/api/config", token=TOKEN).json()
    assert cfg["paperless"]["enabled"] is False and cfg["homeassistant"]["enabled"] is False


def test_office_still_saves_other_settings(office, live_server, app_config):
    r = live_server.post("/api/settings", {"scan": {"resolution": 200}}, token=TOKEN)
    assert r.status == 200
    assert live_server.get("/api/config", token=TOKEN).json()["scan"]["resolution"] == 200


def test_office_has_no_paperless_test_route(office, live_server):
    r = live_server.post("/api/test-paperless", {"url": "http://x"}, token=TOKEN)
    assert r.status == 404


def test_homelab_saves_paperless_settings(live_server, app_config):
    r = live_server.post("/api/settings", {"paperless": {"enabled": True, "mode": "consume"}}, token=TOKEN)
    assert r.status == 200
    assert live_server.get("/api/config", token=TOKEN).json()["paperless"]["enabled"] is True


@pytest.mark.parametrize("page,hidden", [
    ("settings.html", ['id="paperless"', 'id="integrations"']),
    ("wizard.html", ['id="paperless-details"']),
])
def test_pages_mark_home_controls_for_hiding(page, hidden):
    text = (ROOT / "openscan" / "web" / "static" / "templates" / page).read_text()
    for marker in hidden:
        tag = next(line for line in text.splitlines() if marker in line)
        assert "homelab-only" in tag, marker
    common = (ROOT / "openscan" / "web" / "static" / "js" / "common.js").read_text()
    assert "api/edition" in common and ".homelab-only" in common
