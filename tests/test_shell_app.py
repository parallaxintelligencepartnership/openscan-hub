"""App shell: single instance, autostart, logging, startup survival, tray menu."""

import json
import logging
import os
import socket
import subprocess
import sys
import types

import pytest

from openscan import autostart, logging_setup, main as app_main
from openscan.config import get_config_path, load_config
from openscan.singleinstance import InstanceLock, pid_alive


# --- single instance (A7) --------------------------------------------------

def test_lock_acquire_and_release(config_dir):
    lock = InstanceLock(config_dir)
    assert lock.acquire(8020) is None
    data = json.loads((config_dir / "openscan.lock").read_text())
    assert data["pid"] == os.getpid() and data["port"] == 8020
    lock.release()
    assert not (config_dir / "openscan.lock").exists()


class _HealthServer:
    """A tiny HTTP server answering /api/health with a chosen JSON body."""

    def __init__(self, body):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        payload = json.dumps(body).encode()

        class H(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200 if self.path == "/api/health" else 404)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.server.shutdown()
        self.server.server_close()


def _sleeper():
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_second_instance_sees_the_first(config_dir):
    child = _sleeper()
    try:
        with _HealthServer({"status": "ok", "app": "openscanhub", "version": "0.3.0"}) as h:
            (config_dir / "openscan.lock").write_text(json.dumps({"pid": child.pid, "port": h.port}))
            other = InstanceLock(config_dir).acquire(8020)
            assert other is not None and other["port"] == h.port
    finally:
        child.kill()
        child.wait(5)
    # Once that process is gone the lock is stale and can be taken.
    assert not pid_alive(child.pid)
    assert InstanceLock(config_dir).acquire(8020) is None


def test_live_pid_that_is_not_openscanhub_is_stale(config_dir):
    # Windows reuses pids: a lock left by a killed instance can name another
    # live program. Nothing answers health on the port, so start normally.
    child = _sleeper()
    try:
        (config_dir / "openscan.lock").write_text(
            json.dumps({"pid": child.pid, "port": _free_port()}))
        lock = InstanceLock(config_dir)
        assert lock.acquire(8020) is None
        assert json.loads((config_dir / "openscan.lock").read_text())["pid"] == os.getpid()
        lock.release()
        # Something else answering on the port is not OpenScanHub either.
        with _HealthServer({"status": "ok", "app": "someone-else"}) as h:
            (config_dir / "openscan.lock").write_text(
                json.dumps({"pid": child.pid, "port": h.port}))
            assert InstanceLock(config_dir).acquire(8020) is None
    finally:
        child.kill()
        child.wait(5)


def test_health_identifies_the_app(live_server):
    data = live_server.get("/api/health").json()
    assert data["app"] == "openscanhub"


def test_own_pid_in_lock_is_stale(config_dir):
    # Docker restarts reuse pid 1: a lock naming our own pid must not block us.
    (config_dir / "openscan.lock").write_text(json.dumps({"pid": os.getpid(), "port": 1}))
    assert InstanceLock(config_dir).acquire(8020) is None


def test_garbage_lock_file_is_ignored(config_dir):
    (config_dir / "openscan.lock").write_text("not json")
    assert InstanceLock(config_dir).acquire(8020) is None


def test_second_launch_opens_browser_and_exits_0(config_dir, monkeypatch):
    child = _sleeper()
    opened = []
    monkeypatch.setattr(app_main.webbrowser, "open", lambda url: opened.append(url))
    monkeypatch.setattr(app_main, "create_server", None, raising=False)
    try:
        with _HealthServer({"status": "ok", "app": "openscanhub", "version": "0.3.0"}) as h:
            (config_dir / "openscan.lock").write_text(json.dumps({"pid": child.pid, "port": h.port}))
            assert app_main.run([]) == 0
    finally:
        child.kill()
        child.wait(5)
    assert opened == [f"http://localhost:{h.port}/"]


# --- autostart ----------------------------------------------------------------

class FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}

    class _Key:
        def __init__(self, path):
            self.path = path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def OpenKey(self, root, path, reserved, access):
        return self._Key(path)

    def CreateKeyEx(self, root, path, reserved, access):
        return self._Key(path)

    def QueryValueEx(self, key, name):
        if (key.path, name) not in self.values:
            raise FileNotFoundError(name)
        return self.values[(key.path, name)], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        self.values[(key.path, name)] = value

    def DeleteValue(self, key, name):
        if (key.path, name) not in self.values:
            raise FileNotFoundError(name)
        del self.values[(key.path, name)]


def test_autostart_with_fake_winreg(monkeypatch):
    reg = FakeWinreg()
    monkeypatch.setattr(autostart, "_winreg", lambda: reg)
    assert autostart.is_supported()
    assert autostart.is_enabled() is False
    assert autostart.enable('"C:\\OpenScanHub.exe" --no-browser') is True
    assert reg.values[(autostart.RUN_KEY, "OpenScanHub")] == '"C:\\OpenScanHub.exe" --no-browser'
    assert autostart.is_enabled() is True
    assert autostart.disable() is True
    assert autostart.is_enabled() is False
    assert autostart.disable() is True  # already off is fine


def test_autostart_command_for_frozen_exe(monkeypatch):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", "C:\\Apps\\OpenScanHub.exe")
    assert autostart.command() == '"C:\\Apps\\OpenScanHub.exe" --no-browser'


def test_autostart_is_noop_off_windows(monkeypatch):
    monkeypatch.setattr(autostart, "_winreg", lambda: None)
    assert autostart.enable() is False
    assert autostart.disable() is False
    assert autostart.is_enabled() is False


# --- logging (I14) -----------------------------------------------------------------

def test_logging_writes_file_and_ring(config_dir):
    log_dir = logging_setup.setup_logging(level="INFO")
    assert log_dir == config_dir / "logs"
    logging_setup.clear_recent_errors()
    logging.getLogger("openscan.x").info("hello file")
    logging.getLogger("openscan.x").error("it broke")
    for h in logging.getLogger().handlers:
        h.flush()
    text = (log_dir / "openscan.log").read_text()
    assert "hello file" in text and "it broke" in text
    errors = logging_setup.recent_errors()
    assert [e["message"] for e in errors] == ["it broke"]
    assert set(errors[0]) == {"time", "level", "logger", "message"}


def test_logging_without_stderr(config_dir, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)
    logging_setup.setup_logging(level="INFO")
    kinds = {type(h).__name__ for h in logging.getLogger().handlers}
    assert "StreamHandler" not in kinds
    assert "RotatingFileHandler" in kinds
    logging.getLogger("openscan.x").warning("no console")  # must not raise


def test_logging_setup_is_idempotent(config_dir):
    logging_setup.setup_logging(level="INFO")
    before = len(logging.getLogger().handlers)
    logging_setup.setup_logging(level="DEBUG")
    assert len(logging.getLogger().handlers) == before


# --- startup survival -----------------------------------------------------------------

def test_show_token_prints_and_exits(config_dir, capsys):
    assert app_main.run(["--show-token"]) == 0
    token = capsys.readouterr().out.strip()
    assert len(token) == 32
    assert load_config().web.api_token == token


def test_port_in_use_logs_clear_error(config_dir, monkeypatch, caplog):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    port = sock.getsockname()[1]
    try:
        with caplog.at_level(logging.ERROR):
            code = app_main.run(["--no-browser", "--port", str(port)])
    finally:
        sock.close()
    assert code == 1
    assert any("already in use" in r.getMessage() for r in caplog.records)
    assert not (config_dir / "openscan.lock").exists()


def test_malformed_config_is_kept_aside(config_dir):
    path = get_config_path()
    path.write_text("{ this is not json")
    cfg = load_config()
    assert cfg.wizard_completed is False
    backup = path.with_name(path.name + ".broken")
    assert backup.read_text() == "{ this is not json"
    assert json.loads(path.read_text())["web"]["api_token"] == cfg.web.api_token


@pytest.mark.skipif(os.name == "nt" or os.geteuid() == 0, reason="needs POSIX permissions as non-root")
def test_unwritable_config_dir_still_starts(config_dir, capsys):
    os.chmod(config_dir, 0o500)
    try:
        assert app_main.run(["--show-token"]) == 0
        assert len(capsys.readouterr().out.strip()) == 32
        assert logging_setup.get_log_dir() != config_dir / "logs"
    finally:
        os.chmod(config_dir, 0o700)


# --- tray menu ------------------------------------------------------------------------

def test_tray_menu_items(monkeypatch, app_config):
    built = {}

    class Item:
        def __init__(self, text, action, **kw):
            self.text, self.action, self.kw = text, action, kw

    class Menu:
        SEPARATOR = object()

        def __init__(self, *items):
            self.items = items

    class Icon:
        def __init__(self, name, image, title, menu):
            built["menu"] = menu

        def run(self):
            pass

    fake = types.SimpleNamespace(Menu=Menu, MenuItem=Item, Icon=Icon)
    monkeypatch.setitem(sys.modules, "pystray", fake)
    from openscan import tray
    monkeypatch.setattr(tray, "_create_icon_image", lambda: None)
    assert tray.create_tray(app_config) is True
    labels = [i.text for i in built["menu"].items if isinstance(i, Item)]
    assert labels == ["Open dashboard", "Scan from feeder", "Scan from flatbed", "Show access token",
                      "Open log folder", "Start with Windows", "Quit"]
