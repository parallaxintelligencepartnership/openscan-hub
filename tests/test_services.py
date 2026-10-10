"""services.apply_config starts, stops and restarts background services (review I4)."""

import copy

from openscan import services
from openscan.config import AppConfig


def _base() -> AppConfig:
    cfg = AppConfig()
    cfg.wizard_completed = True
    cfg.scanner.ip = "192.0.2.10"
    cfg.scanner.protocol = "escl"
    cfg.output.folder = "/tmp/scans"
    return cfg


def test_start_all_starts_only_what_is_on(fake_services):
    cfg = _base()
    cfg.ftp_receive.enabled = True
    report = services.start_all(cfg)
    started = fake_services.names("start")
    assert "ftp_receive" in started
    assert "retry_queue" in started
    assert "monitor" not in started
    assert "folder_watch" not in started
    assert "mdns" not in started  # bound to loopback
    assert report["errors"] == []
    assert cfg.ftp_receive.staging_dir  # filled in before start


def test_nothing_runs_before_the_wizard(fake_services):
    cfg = _base()
    cfg.wizard_completed = False
    cfg.ftp_receive.enabled = True
    services.start_all(cfg)
    assert fake_services.names("start") == ["retry_queue"]


def test_toggle_starts_and_stops(fake_services):
    old = _base()
    new = copy.deepcopy(old)
    new.folder_watch.enabled = True
    new.folder_watch.watch_folder = "/tmp/inbox"
    report = services.apply_config(old, new)
    assert report["started"] == ["folder watcher"]

    newer = copy.deepcopy(new)
    newer.folder_watch.enabled = False
    report = services.apply_config(new, newer)
    assert report["stopped"] == ["folder watcher"]
    assert fake_services.calls[-1] == ("stop", "openscan.scanner.folder_watch")


def test_monitor_only_with_a_driven_scanner(fake_services):
    old = _base()
    new = copy.deepcopy(old)
    new.monitor.enabled = True
    assert services.apply_config(old, new)["started"] == ["ADF monitor"]
    ftp_only = copy.deepcopy(new)
    ftp_only.scanner.protocol = "ftp"
    ftp_only.scanner.ip = ""
    assert services.apply_config(new, ftp_only)["stopped"] == ["ADF monitor"]


def test_running_service_restarts_when_its_settings_change(fake_services):
    old = _base()
    old.ftp_receive.enabled = True
    new = copy.deepcopy(old)
    new.ftp_receive.port = 2222
    report = services.apply_config(old, new)
    assert report["restarted"] == ["FTP receiver"]
    # An unrelated change does not restart it.
    newer = copy.deepcopy(new)
    newer.homeassistant.enabled = True
    assert services.apply_config(new, newer)["restarted"] == []


def test_destination_change_restarts_delivering_services(fake_services):
    old = _base()
    old.ftp_receive.enabled = True
    new = copy.deepcopy(old)
    new.output.folder = "/tmp/elsewhere"
    assert "FTP receiver" in services.apply_config(old, new)["restarted"]


def test_missing_stop_function_is_tolerated(fake_services):
    mod = fake_services.module("openscan.scanner.ftp_receive")
    del mod.stop_ftp_receiver
    old = _base()
    old.ftp_receive.enabled = True
    new = copy.deepcopy(old)
    new.ftp_receive.enabled = False
    report = services.apply_config(old, new)
    assert report["errors"] == []
    assert report["stopped"] == []


def test_start_failure_is_reported_not_raised(fake_services):
    # The real start functions return False (they do not raise) when they
    # cannot start, and say why through their *_error() function.
    mod = fake_services.module("openscan.scanner.ftp_receive")
    mod.start_ftp_receiver = lambda cfg: False
    mod.ftp_receiver_error = lambda: "port 2121 is in use"
    cfg = _base()
    cfg.ftp_receive.enabled = True
    report = services.start_all(cfg)
    assert "FTP receiver" not in report["started"]
    assert report["failed"] == {"FTP receiver": "FTP receiver could not start: port 2121 is in use"}
    assert report["errors"] == ["FTP receiver could not start: port 2121 is in use"]


def test_raising_start_is_reported(fake_services):
    mod = fake_services.module("openscan.scanner.ftp_receive")

    def boom(cfg):
        raise OSError("disk on fire")

    mod.start_ftp_receiver = boom
    cfg = _base()
    cfg.ftp_receive.enabled = True
    report = services.start_all(cfg)
    assert "disk on fire" in report["failed"]["FTP receiver"]


def test_restart_failure_is_reported(fake_services):
    old = _base()
    old.folder_watch.enabled = True
    old.folder_watch.watch_folder = "/tmp/in"
    new = copy.deepcopy(old)
    new.folder_watch.watch_folder = "/tmp/scans/in"
    mod = fake_services.module("openscan.scanner.folder_watch")
    mod.start_folder_watcher = lambda cfg: False
    mod.folder_watcher_error = lambda: "it is inside the scans folder"
    report = services.apply_config(old, new)
    assert "folder watcher" not in report["restarted"]
    assert report["failed"]["folder watcher"] == \
        "Folder watcher could not start: it is inside the scans folder"


def test_real_ftp_receiver_on_busy_port_is_reported(config_dir, monkeypatch):
    import socket

    busy = socket.socket()
    busy.bind(("0.0.0.0", 0))  # the receiver listens on all interfaces
    busy.listen(1)
    try:
        port = busy.getsockname()[1]
        ftp_svc = [s for s in services.SERVICES if s.name == "FTP receiver"]
        monkeypatch.setattr(services, "SERVICES", ftp_svc)
        cfg = _base()
        cfg.ftp_receive.enabled = True
        cfg.ftp_receive.port = port
        cfg.ftp_receive.password = "pw-12345"
        report = services.start_all(cfg)
        assert report["started"] == []
        assert report["failed"]["FTP receiver"] == \
            f"FTP receiver could not start: port {port} is in use by another program"
    finally:
        busy.close()
        from openscan.scanner import ftp_receive
        ftp_receive.stop_ftp_receiver()


def test_missing_module_is_tolerated(monkeypatch):
    def load(modpath):
        raise ImportError(modpath)

    monkeypatch.setattr(services, "_load", load)
    cfg = _base()
    cfg.ftp_receive.enabled = True
    report = services.start_all(cfg)
    assert report["started"] == [] and report["errors"] == []


def test_mdns_follows_running_bind(fake_services, monkeypatch):
    cfg = _base()
    services.set_running_web("0.0.0.0", 8020)
    services.start_all(cfg)
    assert "mdns" in fake_services.names("start")
    new = copy.deepcopy(cfg)
    new.web.advertise_mdns = False
    assert "network name (mDNS)" in services.apply_config(cfg, new)["stopped"]


def test_restart_needed_for_bind_or_port(fake_services):
    cfg = _base()
    services.set_running_web("127.0.0.1", 8020)
    assert services.apply_config(cfg, copy.deepcopy(cfg))["restart_needed"] is False
    new = copy.deepcopy(cfg)
    new.web.bind = "0.0.0.0"
    assert services.apply_config(cfg, new)["restart_needed"] is True


def test_stop_all_calls_every_stop(fake_services):
    services.stop_all()
    stopped = set(fake_services.names("stop"))
    assert {"monitor", "folder_watch", "ftp_receive", "retry_queue", "mdns"} <= stopped


def test_mdns_module_registers_and_unregisters(monkeypatch):
    from openscan import mdns

    events = []

    class FakeZeroconf:
        def register_service(self, info, allow_name_change=False):
            events.append(("register", info.port))

        def unregister_service(self, info):
            events.append(("unregister", info.port))

        def close(self):
            events.append(("close", None))

    import zeroconf
    monkeypatch.setattr(zeroconf, "Zeroconf", FakeZeroconf)
    monkeypatch.setattr(mdns, "_local_ipv4", lambda bind: ["192.168.1.20"])
    assert mdns.start(port=8020, bind="0.0.0.0") is True
    assert mdns.running()
    mdns.stop()
    mdns.stop()  # second stop is a no-op
    assert events == [("register", 8020), ("unregister", 8020), ("close", None)]
    assert not mdns.running()
