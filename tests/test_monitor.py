"""ADF monitor: exclusivity with other scans, lifecycle, offline handling."""

import logging
import threading
import time
from dataclasses import dataclass, field

import pytest

from openscan import feed, monitor, multipage
from openscan.config import AppConfig
from openscan.profiles import Profile
from openscan.scanner.base import AdfState, ScannerState, ScannerStatus, ScanSource, scan_lock


class FakeProtocol:
    def __init__(self):
        self.adf = AdfState.EMPTY
        self.state = ScannerState.IDLE
        self.scans = []
        self.lock_free_during_scan = []
        self.polls = 0

    def get_status(self, info):
        self.polls += 1
        return ScannerStatus(state=self.state, adf_state=self.adf)

    def scan(self, info, settings):
        # Is the global lock held (by the monitor) while we scan?
        free = []
        t = threading.Thread(target=lambda: free.append(scan_lock.acquire(blocking=False)
                                                        and (scan_lock.release() or True)))
        t.start()
        t.join(2)
        self.lock_free_during_scan.append(free[0])
        self.scans.append(settings)
        self.adf = AdfState.EMPTY
        return b"%PDF-1.4 fake"


@dataclass
class Delivery:
    ok: bool
    saved_paths: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    queued: bool = False


def _wait_for(pred, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def setup(monkeypatch, tmp_path):
    fake = FakeProtocol()
    saved = []
    monkeypatch.setattr(monitor, "_make_impl", lambda proto: fake)
    monkeypatch.setattr(monitor.intake, "handle_scan", lambda cfg, pdf, **kw: saved.append({"config": cfg, "pdf_data": pdf, **kw}) or Delivery(ok=True))
    monkeypatch.setattr(monitor, "MAX_BACKOFF", 0.2)
    multipage._sessions.clear()
    cfg = AppConfig()
    cfg.scanner.protocol = "escl"
    cfg.scanner.ip = "127.0.0.1"
    cfg.output.folder = str(tmp_path)
    cfg.monitor.enabled = True
    cfg.monitor.poll_interval = 0.02
    cfg.monitor.settle_time = 0.01
    cfg.scan.color_mode = "gray"
    cfg.scan.remove_blank_pages = True
    yield fake, saved, cfg
    monitor.stop_monitor()
    multipage._sessions.clear()


def test_paper_loaded_triggers_one_scan_with_lock_held(setup):
    fake, saved, cfg = setup
    monitor.start_monitor(cfg)
    assert _wait_for(lambda: fake.polls >= 2)
    fake.adf = AdfState.LOADED
    assert _wait_for(lambda: len(saved) == 1)
    time.sleep(0.1)
    assert len(fake.scans) == 1
    s = fake.scans[0]
    assert s.source == ScanSource.ADF and s.color_mode == "gray" and s.remove_blank_pages
    assert fake.lock_free_during_scan == [False]


def test_paper_already_loaded_at_start_is_not_scanned(setup):
    fake, saved, cfg = setup
    fake.adf = AdfState.LOADED
    monitor.start_monitor(cfg)
    assert _wait_for(lambda: fake.polls >= 5)
    assert fake.scans == []


def test_skips_while_another_scan_holds_lock(setup):
    fake, saved, cfg = setup
    monitor.start_monitor(cfg)
    assert _wait_for(lambda: fake.polls >= 2)
    held, release = threading.Event(), threading.Event()

    def manual_scan():
        with scan_lock:
            held.set()
            release.wait(5)

    t = threading.Thread(target=manual_scan)
    t.start()
    held.wait(2)
    polls = fake.polls
    fake.adf = AdfState.LOADED
    time.sleep(0.3)
    assert fake.scans == []
    assert fake.polls <= polls + 1  # at most one poll that raced the lock
    release.set()
    t.join(2)
    fake.adf = AdfState.EMPTY
    assert _wait_for(lambda: fake.polls >= polls + 3)
    fake.adf = AdfState.LOADED
    assert _wait_for(lambda: len(fake.scans) == 1)


def test_never_scans_during_multipage_session(setup):
    fake, saved, cfg = setup
    sid = multipage.create_session(ScanSource.FLATBED)
    monitor.start_monitor(cfg)
    time.sleep(0.1)
    fake.adf = AdfState.LOADED
    time.sleep(0.3)
    assert fake.scans == []
    fake.adf = AdfState.EMPTY
    multipage.delete_session(sid)
    polls = fake.polls
    assert _wait_for(lambda: fake.polls >= polls + 3)
    fake.adf = AdfState.LOADED
    assert _wait_for(lambda: len(fake.scans) == 1)


def test_offline_logs_once_and_recovers(setup, caplog):
    fake, saved, cfg = setup
    caplog.set_level(logging.INFO, logger="openscan.monitor")
    fake.state = ScannerState.OFFLINE
    monitor.start_monitor(cfg)
    assert _wait_for(lambda: fake.polls >= 5)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1 and "cannot reach" in warnings[0].getMessage()
    fake.state = ScannerState.IDLE
    assert _wait_for(lambda: any("responding again" in r.getMessage() for r in caplog.records))
    polls = fake.polls
    assert _wait_for(lambda: fake.polls >= polls + 2)
    fake.adf = AdfState.LOADED
    assert _wait_for(lambda: len(fake.scans) == 1)
    assert monitor.monitor_running()


def test_status_exception_does_not_kill_thread(setup, caplog):
    fake, saved, cfg = setup
    calls = {"n": 0}
    real = fake.get_status

    def flaky(info):
        calls["n"] += 1
        if calls["n"] < 4:
            raise ValueError("bad xml")
        return real(info)

    fake.get_status = flaky
    monitor.start_monitor(cfg)
    assert _wait_for(lambda: fake.polls >= 2)
    assert monitor.monitor_running()


def test_lifecycle_idempotent_and_disabled(setup):
    fake, saved, cfg = setup
    monitor.stop_monitor()  # never started: returns
    assert not monitor.monitor_running()
    monitor.start_monitor(cfg)
    first = monitor._thread
    monitor.start_monitor(cfg)
    assert monitor._thread is first and monitor.monitor_running()
    cfg.monitor.enabled = False
    t0 = time.monotonic()
    monitor.start_monitor(cfg)
    assert not monitor.monitor_running() and time.monotonic() - t0 < 5
    monitor.start_monitor(cfg)
    assert not monitor.monitor_running()


def test_failed_delivery_is_logged(setup, monkeypatch, caplog):
    fake, saved, cfg = setup
    monkeypatch.setattr(monitor.intake, "handle_scan", lambda cfg, pdf, **kw: Delivery(ok=False, errors=["NAS offline"]))
    caplog.set_level(logging.ERROR, logger="openscan.monitor")
    monitor.start_monitor(cfg)
    assert _wait_for(lambda: fake.polls >= 2)
    fake.adf = AdfState.LOADED
    assert _wait_for(lambda: any("NAS offline" in r.getMessage() for r in caplog.records))


def test_delivery_ok_duck_typing():
    assert monitor._delivery_ok(Delivery(ok=True))
    assert not monitor._delivery_ok(Delivery(ok=False))
    assert monitor._delivery_ok({"saved": [{"path": "x"}], "errors": []})
    assert not monitor._delivery_ok({"saved": [], "errors": [{"error": "x"}]})


def test_wsd_without_feeder_state_logs_once(setup, caplog):
    fake, saved, cfg = setup
    cfg.scanner.protocol = "wsd"
    fake.adf = AdfState.UNKNOWN
    caplog.set_level(logging.INFO, logger="openscan.monitor")
    monitor.start_monitor(cfg)
    assert _wait_for(lambda: fake.polls >= 5)
    notes = [r for r in caplog.records if "WSD" in r.getMessage()]
    assert len(notes) == 1
    assert notes[0].levelno == logging.WARNING
    assert "feeder" in notes[0].getMessage()


def test_scanner_lock_is_released_before_delivery(setup, monkeypatch):
    # Delivery can wait a minute for Paperless; dashboard scans must not get
    # "busy" all that time.
    fake, saved, cfg = setup
    lock_free_during_delivery = []

    def deliver(cfg_, pdf, **kw):
        free = []  # ask from another thread: the lock may be reentrant
        t = threading.Thread(target=lambda: free.append(scan_lock.acquire(blocking=False)
                                                        and (scan_lock.release() or True)))
        t.start()
        t.join(2)
        lock_free_during_delivery.append(free[0])
        return Delivery(ok=True)

    monkeypatch.setattr(monitor.intake, "handle_scan", deliver)
    monitor.start_monitor(cfg)
    assert _wait_for(lambda: fake.polls >= 2)
    fake.adf = AdfState.LOADED
    assert _wait_for(lambda: len(lock_free_during_delivery) == 1)
    assert lock_free_during_delivery == [True]
    assert fake.lock_free_during_scan == [False]


# --- next feed profile ---------------------------------------------------------

def _two_profiles(cfg, tmp_path):
    cfg.profiles = [
        Profile(id="default", name="Default", color_mode="color",
                destinations=[{"type": "folder", "options": {"folder": str(tmp_path / "d")}}]),
        Profile(id="photos", name="Photos", color_mode="gray", resolution=600,
                destinations=[{"type": "folder", "options": {"folder": str(tmp_path / "p")}}]),
    ]
    cfg.default_profile_id = "default"


def _feed_one_batch(fake, saved, n):
    polls = fake.polls
    assert _wait_for(lambda: fake.polls >= polls + 3)  # the monitor sees an empty feeder first
    fake.adf = AdfState.LOADED
    assert _wait_for(lambda: len(saved) == n)


def test_next_feed_profile_is_used_once_then_default(setup, tmp_path):
    fake, saved, cfg = setup
    _two_profiles(cfg, tmp_path)
    feed.set_next("photos")
    monitor.start_monitor(cfg)
    _feed_one_batch(fake, saved, 1)
    first = fake.scans[0]
    assert (first.color_mode, first.resolution) == ("gray", 600)
    assert saved[0]["profile"].id == "photos"
    assert saved[0]["profile"].destinations[0]["options"]["folder"].endswith("p")
    assert saved[0]["started_by"] == "feeder monitor"
    assert _wait_for(lambda: feed.peek() is None)  # cleared after delivery
    _feed_one_batch(fake, saved, 2)
    second = fake.scans[1]
    assert (second.color_mode, second.resolution) == ("color", 300)
    assert saved[1]["profile"].id == "default"
    assert saved[1]["profile"].destinations[0]["options"]["folder"].endswith("d")


def test_next_feed_expired_falls_back_to_default(setup, tmp_path, monkeypatch):
    fake, saved, cfg = setup
    _two_profiles(cfg, tmp_path)
    clock = [1000.0]
    monkeypatch.setattr(feed, "_now", lambda: clock[0])
    feed.set_next("photos")
    clock[0] += feed.TTL_SECONDS + 1
    monitor.start_monitor(cfg)
    _feed_one_batch(fake, saved, 1)
    assert saved[0]["profile"].id == "default"


def test_next_feed_stays_when_delivery_raises(setup, tmp_path, monkeypatch):
    fake, saved, cfg = setup
    _two_profiles(cfg, tmp_path)

    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(monitor.intake, "handle_scan", boom)
    feed.set_next("photos")
    monitor.start_monitor(cfg)
    assert _wait_for(lambda: fake.polls >= 2)
    fake.adf = AdfState.LOADED
    assert _wait_for(lambda: len(fake.scans) == 1)
    time.sleep(0.2)
    assert feed.next_profile_id() == "photos"


def test_next_feed_changed_during_scan_is_kept(setup, tmp_path, monkeypatch):
    fake, saved, cfg = setup
    _two_profiles(cfg, tmp_path)
    feed.set_next("default")
    real = monitor.intake.handle_scan

    def deliver(cfg_, pdf, **kw):
        feed.set_next("photos")  # the user picks again while this batch is delivered
        return real(cfg_, pdf, **kw)

    monkeypatch.setattr(monitor.intake, "handle_scan", deliver)
    monitor.start_monitor(cfg)
    assert _wait_for(lambda: fake.polls >= 2)
    fake.adf = AdfState.LOADED
    assert _wait_for(lambda: len(saved) == 1)
    time.sleep(0.2)
    assert feed.next_profile_id() == "photos"
