"""Shared fixtures: a temp config dir, an AppConfig, fake services, and a live
web server on an ephemeral port whose peer address can be faked."""

import http.client
import json
import threading
import types
from dataclasses import dataclass, field
from typing import Optional

import pytest

from openscan.config import AppConfig

TOKEN = "0123456789abcdef0123456789abcdef"
LAN_PEER = "192.168.1.50"

_ENV = ("OPENSCAN_BIND", "OPENSCAN_PORT", "OPENSCAN_TOKEN", "OPENSCAN_TRUST_PROXY_IP",
        "OPENSCAN_ALLOWED_HOSTS", "OPENSCAN_DOCKER", "OPENSCAN_DISABLE_LOCAL_AUTOLOGIN",
        "OPENSCAN_FTP_PASSIVE_ADDRESS")


@pytest.fixture(autouse=True)
def _restore_root_logging():
    """setup_logging() replaces root handlers; put them back after each test."""
    import logging
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    from openscan import logging_setup
    for h in list(root.handlers):
        if h not in handlers:
            root.removeHandler(h)
            if h is not logging_setup._ring:
                h.close()
    for h in handlers:
        if h not in root.handlers:
            root.addHandler(h)
    root.setLevel(level)
    logging_setup._handlers.clear()


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    """An empty config folder used by get_config_dir()."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    d = tmp_path / "config"
    d.mkdir()
    monkeypatch.setenv("OPENSCAN_CONFIG_DIR", str(d))
    return d


@pytest.fixture
def app_config(config_dir, tmp_path) -> AppConfig:
    cfg = AppConfig()
    cfg.wizard_completed = True
    cfg.web.bind = "127.0.0.1"
    cfg.web.port = 0
    cfg.web.api_token = TOKEN
    cfg.web.advertise_mdns = False
    cfg.ftp_receive.password = "ftp-secret-pw"
    cfg.output.folder = str(tmp_path / "scans")
    cfg.scanner.ip = "192.0.2.10"
    cfg.scanner.port = 80
    cfg.scanner.protocol = "escl"
    cfg.scanner.name = "Test Scanner"
    return cfg


class FakeServiceModule(types.SimpleNamespace):
    pass


@dataclass
class FakeServices:
    calls: list = field(default_factory=list)
    modules: dict = field(default_factory=dict)

    def module(self, modpath: str):
        if modpath not in self.modules:
            short = modpath.rsplit(".", 1)[-1]
            ns = FakeServiceModule()
            names = {
                "monitor": ("start_monitor", "stop_monitor"),
                "folder_watch": ("start_folder_watcher", "stop_folder_watcher"),
                "ftp_receive": ("start_ftp_receiver", "stop_ftp_receiver"),
                "mdns": ("start", "stop"),
            }.get(short)
            if short == "retry_queue":
                queue = types.SimpleNamespace(
                    start=lambda cfg, _m=modpath: self.calls.append(("start", _m)),
                    stop=lambda _m=modpath: self.calls.append(("stop", _m)),
                    pending=lambda: [],
                )
                ns.get_queue = lambda: queue
            elif names:
                start, stop = names
                setattr(ns, start, lambda *a, _m=modpath, **k: self.calls.append(("start", _m)))
                setattr(ns, stop, lambda _m=modpath: self.calls.append(("stop", _m)))
            self.modules[modpath] = ns
        return self.modules[modpath]

    def names(self, action: str) -> list[str]:
        return [m.rsplit(".", 1)[-1] for a, m in self.calls if a == action]


@pytest.fixture
def fake_services(monkeypatch):
    from openscan import services

    fakes = FakeServices()
    monkeypatch.setattr(services, "_load", fakes.module)
    monkeypatch.setattr(services, "_running_web", None)
    return fakes


@dataclass
class Response:
    status: int
    headers: http.client.HTTPMessage
    body: bytes

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def cookie(self) -> Optional[str]:
        raw = self.headers.get("Set-Cookie")
        if not raw:
            return None
        return raw.split(";", 1)[0]


class LiveServer:
    def __init__(self, server):
        self.server = server
        self.port = server.server_address[1]

    def set_peer(self, ip: Optional[str]) -> None:
        """Pretend requests come from `ip` (None = the real loopback peer)."""
        self.server.peer_override = ip

    def request(self, method: str, path: str, *, headers: Optional[dict] = None,
                json_body=None, body: Optional[bytes] = None, host: Optional[str] = None,
                token: Optional[str] = None, cookie: Optional[str] = None) -> Response:
        hdrs = {"Host": host or f"localhost:{self.port}"}
        if json_body is not None:
            body = json.dumps(json_body).encode()
            hdrs["Content-Type"] = "application/json"
        if token:
            hdrs["X-OpenScan-Token"] = token
        if cookie:
            hdrs["Cookie"] = cookie
        hdrs.update(headers or {})
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            conn.putrequest(method, path, skip_host=True, skip_accept_encoding=True)
            for k, v in hdrs.items():
                conn.putheader(k, v)
            if body is not None:
                conn.putheader("Content-Length", str(len(body)))
            conn.endheaders(body)
            resp = conn.getresponse()
            return Response(resp.status, resp.headers, resp.read())
        finally:
            conn.close()

    def get(self, path: str, **kw) -> Response:
        return self.request("GET", path, **kw)

    def post(self, path: str, json_body=None, **kw) -> Response:
        return self.request("POST", path, json_body={} if json_body is None and "body" not in kw else json_body, **kw)


class FakeScanner:
    """Stands in for EsclScanner/WsdScanner. Offline unless told otherwise."""

    def __init__(self):
        from openscan.scanner.base import ScanSource
        self.online = False
        self.sources = [ScanSource.FLATBED]
        self.data = b"%PDF-1.4 fake"
        self.gate: Optional[threading.Event] = None  # scan() waits on this if set
        self.scans = []

    def get_status(self, info):
        from openscan.scanner.base import ScannerState, ScannerStatus
        if not self.online:
            raise OSError("offline")
        return ScannerStatus(state=ScannerState.IDLE)

    def get_capabilities(self, info):
        from openscan.scanner.base import ScannerCapabilities
        if not self.online:
            raise OSError("offline")
        return ScannerCapabilities(sources=list(self.sources))

    def scan(self, info, settings):
        self.scans.append(settings)
        if self.gate is not None:
            self.gate.wait(5)
        return self.data


@pytest.fixture
def fake_scanner(monkeypatch):
    from openscan.web import dashboard_api

    scanner = FakeScanner()
    monkeypatch.setattr(dashboard_api, "_impl", lambda protocol: scanner)
    monkeypatch.setattr(dashboard_api.multipage, "ensure_pdf", lambda data, resolution=300: data)
    dashboard_api.clear_caps_cache()
    dashboard_api._online_cache.update(time=0.0, value=None, key=None)
    yield scanner
    dashboard_api.clear_caps_cache()


@pytest.fixture
def live_server(app_config, fake_services, fake_scanner):
    from openscan.web import server as srv

    server = srv.create_server(app_config)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
    thread.start()
    live = LiveServer(server)
    try:
        yield live
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
