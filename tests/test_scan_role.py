"""Two tokens: the admin token opens everything, the scan token only the scan routes."""

import pytest
from conftest import LAN_PEER, TOKEN

from openscan import audit
from openscan.config import AppConfig, load_config
from openscan.web import server as srv

SCAN_TOKEN = "fedcba9876543210fedcba9876543210"


@pytest.fixture
def scan_server(live_server, app_config):
    app_config.web.scan_token = SCAN_TOKEN
    live_server.set_peer(LAN_PEER)
    return live_server


def test_allow_list_matches_the_design():
    allows = srv.scan_role_allows
    assert allows("GET", "/api/profiles")
    assert allows("POST", "/api/profiles/default/scan")
    assert allows("POST", "/api/scan")
    for sub, method in (("start", "POST"), ("scan", "POST"), ("save", "POST"), ("cancel", "POST"),
                        ("thumbnail/abc/0", "GET"), ("status/abc", "GET")):
        assert allows(method, f"/api/multipage/{sub}")
    for path in ("/api/status", "/api/health", "/api/edition"):
        assert allows("GET", path)
    for method, path in (("POST", "/api/settings"), ("GET", "/api/config"), ("GET", "/api/secrets"),
                         ("GET", "/api/audit"), ("GET", "/api/audit/download"),
                         ("POST", "/api/token/regenerate"), ("POST", "/api/scan-token/regenerate"),
                         ("GET", "/api/history"), ("GET", "/api/queue"), ("GET", "/api/errors"),
                         ("POST", "/api/profiles"), ("POST", "/api/profiles/a/b/scan"),
                         ("POST", "/api/save-config"), ("POST", "/api/discover"),
                         ("GET", "/api/profiles/default"), ("POST", "/api/status")):
        assert not allows(method, path), (method, path)


def test_scan_token_reaches_the_allowed_routes(scan_server, fake_scanner):
    fake_scanner.online = True
    assert scan_server.get("/api/status", token=SCAN_TOKEN).status == 200
    assert scan_server.get("/api/health", token=SCAN_TOKEN).status == 200
    edition = scan_server.get("/api/edition", token=SCAN_TOKEN)
    assert edition.status == 200 and edition.json()["role"] == "scan"
    assert scan_server.get("/api/multipage/status/none", token=SCAN_TOKEN).status == 404


def test_scan_token_scans_and_audit_records_the_role(scan_server, fake_scanner, app_config):
    app_config.audit.enabled = True
    fake_scanner.online = True
    r = scan_server.post("/api/scan", {"source": "flatbed"}, token=SCAN_TOKEN)
    assert r.status == 200 and r.json()["success"]
    assert audit.recent(1)[0]["started_by"] == f"web scan {LAN_PEER}"
    scan_server.post("/api/scan", {"source": "flatbed"}, token=TOKEN)
    assert audit.recent(1)[0]["started_by"] == f"web admin {LAN_PEER}"


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/config"), ("GET", "/api/secrets"), ("POST", "/api/settings"),
    ("POST", "/api/token/regenerate"), ("GET", "/api/audit"), ("GET", "/api/audit/download"),
    ("GET", "/api/history"), ("GET", "/api/queue"), ("POST", "/api/discover"),
])
def test_scan_token_is_refused_elsewhere_with_a_plain_message(scan_server, method, path):
    r = scan_server.request(method, path, token=SCAN_TOKEN, json_body={} if method == "POST" else None)
    assert r.status == 403
    assert "only scan" in r.json()["error"]


def test_admin_token_still_opens_everything(scan_server):
    assert scan_server.get("/api/config", token=TOKEN).status == 200
    assert scan_server.get("/api/secrets", token=TOKEN).json()["scan_token"] == SCAN_TOKEN
    assert scan_server.get("/api/audit", token=TOKEN).status == 200


def test_wrong_and_empty_scan_tokens_get_401(scan_server, app_config):
    assert scan_server.get("/api/status", token="nope" * 8).status == 401
    app_config.web.scan_token = ""
    assert scan_server.get("/api/status", token="").status == 401


def test_scan_cookie_opens_only_the_scan_page(scan_server):
    login = scan_server.post("/api/login", {"token": SCAN_TOKEN})
    assert login.status == 200 and login.json()["role"] == "scan"
    cookie = login.cookie()
    assert cookie and cookie != scan_server.post("/api/login", {"token": TOKEN}).cookie()
    assert scan_server.get("/api/status", cookie=cookie).status == 200
    assert scan_server.get("/api/config", cookie=cookie).status == 403
    assert scan_server.get("/dashboard", cookie=cookie).status == 200
    for page in ("/settings", "/wizard", "/"):
        r = scan_server.get(page, cookie=cookie)
        assert r.status == 302 and r.headers["Location"] == "dashboard"


def test_admin_cookie_opens_settings(scan_server):
    cookie = scan_server.post("/api/login", {"token": TOKEN}).cookie()
    assert scan_server.get("/settings", cookie=cookie).status == 200


def test_scan_token_is_secret_in_config_and_read_only(scan_server, app_config):
    data = scan_server.get("/api/config", token=TOKEN).json()
    assert data["web"]["scan_token"] == "" and data["web"]["has_scan_token"]
    scan_server.post("/api/settings", {"web": {"scan_token": "mine"}}, token=TOKEN)
    assert scan_server.get("/api/secrets", token=TOKEN).json()["scan_token"] == SCAN_TOKEN


def test_regenerate_scan_token(scan_server, app_config):
    r = scan_server.post("/api/scan-token/regenerate", {}, token=TOKEN)
    new = r.json()["scan_token"]
    assert new != SCAN_TOKEN and len(new) == 32
    assert scan_server.get("/api/status", token=SCAN_TOKEN).status == 401
    assert scan_server.get("/api/status", token=new).status == 200


def test_load_config_generates_a_scan_token_once(config_dir):
    first = load_config()
    assert len(first.web.scan_token) == 32 and first.web.scan_token != first.web.api_token
    assert load_config().web.scan_token == first.web.scan_token


def test_show_token_scan(config_dir, capsys):
    from openscan import main as app_main
    cfg = load_config()
    assert app_main.run(["--show-token", "--scan"]) == 0
    assert capsys.readouterr().out.strip() == cfg.web.scan_token
    assert app_main.run(["--show-token"]) == 0
    assert capsys.readouterr().out.strip() == cfg.web.api_token


def test_default_config_has_no_scan_token():
    assert AppConfig().web.scan_token == ""
