"""Auth contract for the web UI and API (review finding C1)."""

from conftest import LAN_PEER, TOKEN

from openscan.web.server import SESSION_COOKIE, session_value


def test_health_is_open_from_lan(live_server, monkeypatch):
    from openscan.web import dashboard_api
    monkeypatch.setattr(dashboard_api, "_impl", _offline_impl)
    live_server.set_peer(LAN_PEER)
    r = live_server.get("/api/health")
    assert r.status == 200
    data = r.json()
    assert data["status"] == "ok"
    assert set(data) >= {"version", "scanner_online", "queue_pending", "uptime_seconds"}
    assert data["scanner_online"] is False


def test_lan_peer_without_token_gets_401(live_server):
    live_server.set_peer(LAN_PEER)
    assert live_server.get("/api/status").status == 401
    assert live_server.get("/api/config").status == 401
    assert live_server.post("/api/settings", {"output": {"folder": "/x"}}).status == 401


def test_loopback_peer_without_cookie_is_not_trusted_on_api(live_server):
    # A malicious page in the user's own browser is also a loopback peer.
    assert live_server.get("/api/config").status == 401


def test_token_header_and_bearer_work(live_server, monkeypatch):
    from openscan.web import dashboard_api
    monkeypatch.setattr(dashboard_api, "_impl", _offline_impl)
    live_server.set_peer(LAN_PEER)
    assert live_server.get("/api/config", token=TOKEN).status == 200
    r = live_server.get("/api/config", headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status == 200
    assert live_server.get("/api/config", token="wrong" * 6).status == 401


def test_query_token_only_on_module_routes(live_server):
    live_server.set_peer(LAN_PEER)
    assert live_server.get(f"/api/config?token={TOKEN}").status == 401


def test_bad_host_is_403(live_server):
    r = live_server.get("/api/health", host="evil.example:8020")
    assert r.status == 403
    # A token does not help: the Host check runs first.
    assert live_server.get("/api/config", host="evil.example", token=TOKEN).status == 403
    assert live_server.get("/dashboard", host="rebind.attacker.test").status == 403


def test_allowed_hosts(live_server, app_config):
    assert live_server.get("/api/health", host="127.0.0.1:8020").status == 200
    assert live_server.get("/api/health", host="[::1]:8020").status == 200
    assert live_server.get("/api/health", host="openscanhub.local").status == 200
    # A foreign bare IP is refused while bound to loopback ...
    assert live_server.get("/api/health", host="10.255.255.1").status == 403
    # ... but allowed in LAN mode, as are .local names.
    app_config.web.bind = "0.0.0.0"
    assert live_server.get("/api/health", host="10.255.255.1:8020").status == 200
    assert live_server.get("/api/health", host="mypc.local").status == 200
    assert live_server.get("/api/health", host="evil.example").status == 403


def test_extra_allowed_hosts_env(live_server, monkeypatch):
    monkeypatch.setenv("OPENSCAN_ALLOWED_HOSTS", "nas, scanbox")
    assert live_server.get("/api/health", host="scanbox:8020").status == 200


def test_cross_site_origin_post_is_403(live_server):
    r = live_server.post("/api/settings", {}, token=TOKEN,
                         headers={"Origin": "https://evil.example"})
    assert r.status == 403
    port = live_server.port
    ok = live_server.post("/api/settings", {}, token=TOKEN,
                          headers={"Origin": f"http://localhost:{port}"})
    assert ok.status == 200


def test_text_plain_post_is_415(live_server):
    r = live_server.request("POST", "/api/settings", body=b'{"output":{}}', token=TOKEN,
                            headers={"Content-Type": "text/plain"})
    assert r.status == 415


def test_loopback_page_load_issues_cookie(live_server):
    r = live_server.get("/dashboard")
    assert r.status == 200
    raw = r.headers.get("Set-Cookie")
    assert raw and SESSION_COOKIE in raw
    assert "HttpOnly" in raw and "SameSite=Strict" in raw
    cookie = r.cookie()
    assert TOKEN not in cookie
    assert cookie == f"{SESSION_COOKIE}={session_value(TOKEN)}"
    assert live_server.get("/api/config", cookie=cookie).status == 200


def test_lan_page_load_redirects_to_login(live_server):
    live_server.set_peer(LAN_PEER)
    r = live_server.get("/dashboard")
    assert r.status == 302
    assert r.headers["Location"] == "login"
    assert r.headers.get("Set-Cookie") is None
    assert live_server.get("/login").status == 200


def test_login_with_token_sets_cookie(live_server):
    live_server.set_peer(LAN_PEER)
    bad = live_server.post("/api/login", {"token": "nope"})
    assert bad.status == 401
    good = live_server.post("/api/login", {"token": TOKEN})
    assert good.status == 200
    cookie = good.cookie()
    assert "Max-Age=2592000" in good.headers["Set-Cookie"]
    assert live_server.get("/api/config", cookie=cookie).status == 200
    assert live_server.get("/dashboard", cookie=cookie).status == 200


def test_trusted_proxy_needs_no_token(live_server, monkeypatch):
    monkeypatch.setenv("OPENSCAN_TRUST_PROXY_IP", "172.30.32.2")
    live_server.set_peer("172.30.32.2")
    assert live_server.get("/api/config", host="some-addon-host").status == 200
    live_server.set_peer(LAN_PEER)
    assert live_server.get("/api/config").status == 401


def test_ingress_path_prefixes_redirects(live_server):
    r = live_server.get("/", headers={"X-Ingress-Path": "/api/hassio_ingress/abc"})
    assert r.status == 302
    assert r.headers["Location"] == "/api/hassio_ingress/abc/dashboard"


def test_old_get_scan_routes_are_gone(live_server):
    live_server.set_peer(LAN_PEER)
    assert live_server.get("/api/scan/adf", token=TOKEN).status == 404
    assert live_server.get("/api/scan/flatbed", token=TOKEN).status == 404
    assert live_server.get("/api/scan", token=TOKEN).status == 405


def test_static_traversal_is_403(live_server):
    assert live_server.get("/../config.py").status == 403
    assert live_server.get("/css/../../server.py").status == 403
    assert live_server.get("/css/style.css").status == 200


def test_bad_json_is_400(live_server):
    r = live_server.request("POST", "/api/settings", body=b"{not json", token=TOKEN,
                            headers={"Content-Type": "application/json"})
    assert r.status == 400


class _OfflineImpl:
    def get_status(self, info):
        raise OSError("offline")

    def get_capabilities(self, info):
        raise OSError("offline")


def _offline_impl(protocol):
    return _OfflineImpl()


def test_proxied_loopback_request_gets_no_automatic_cookie(live_server, monkeypatch):
    # A reverse proxy on the same machine connects from 127.0.0.1; the proxy
    # headers show the visitor is somewhere else, so no automatic sign-in.
    monkeypatch.setenv("OPENSCAN_ALLOWED_HOSTS", "scans.example.com")
    for name, value in (("X-Forwarded-For", "203.0.113.9"), ("X-Real-IP", "203.0.113.9"),
                        ("Forwarded", "for=203.0.113.9"),
                        ("X-Forwarded-Host", "scans.example.com")):
        r = live_server.get("/dashboard", host="scans.example.com", headers={name: value})
        assert r.status == 302, name
        assert r.headers["Location"] == "login"
        assert r.headers.get("Set-Cookie") is None
        r = live_server.get("/login", headers={name: value})
        assert r.status == 200, name
        assert r.headers.get("Set-Cookie") is None
    # The token still works through the proxy.
    r = live_server.get("/api/config", token=TOKEN, headers={"X-Forwarded-For": "203.0.113.9"})
    assert r.status == 200


def test_local_autologin_can_be_disabled(live_server, monkeypatch):
    monkeypatch.setenv("OPENSCAN_DISABLE_LOCAL_AUTOLOGIN", "1")
    r = live_server.get("/dashboard")
    assert r.status == 302
    assert r.headers.get("Set-Cookie") is None
    assert live_server.get("/login").status == 200


def test_non_ascii_token_is_just_wrong(live_server):
    live_server.set_peer(LAN_PEER)
    r = live_server.get("/api/config", headers={"X-OpenScan-Token": "é" * 8})
    assert r.status == 401
    r = live_server.get("/dashboard", cookie=f"{SESSION_COOKIE}=éé")
    assert r.status == 302
    r = live_server.post("/api/login", {"token": "éè"})
    assert r.status == 401
