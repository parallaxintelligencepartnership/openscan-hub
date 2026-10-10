import json
import time

import pytest

from openscan import retry_queue
from openscan.retry_queue import BACKOFF, DAILY, MAX_AGE, RetryQueue, backoff_delay

PDF = b"%PDF-1.4 retry\n%%EOF"


class Clock:
    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


def make(tmp_path, clock):
    return RetryQueue(tmp_path / "retry_queue.json", tmp_path / "retry_files", clock=clock)


def test_backoff_schedule():
    assert [backoff_delay(n) for n in range(1, 8)] == [60, 300, 900, 3600, DAILY, DAILY, DAILY]
    assert BACKOFF == [60, 300, 900, 3600]


def test_persistence(tmp_path, clock):
    q = make(tmp_path, clock)
    out = tmp_path / "out"
    [eid] = q.add(filename="a.pdf", destinations=[("folder", str(out))], pdf_data=PDF, error="offline")
    q2 = make(tmp_path, clock)
    [entry] = q2.pending()
    assert entry["id"] == eid
    assert set(entry) >= {"id", "file", "destination", "attempts", "last_error", "next_try"}
    assert entry["attempts"] == 1 and entry["last_error"] == "offline"
    data = json.loads((tmp_path / "retry_queue.json").read_text())
    assert data["entries"][0]["next_try_ts"] == clock.now + 60


def test_run_due_follows_backoff_then_delivers(tmp_path, clock):
    q = make(tmp_path, clock)
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    target = blocker / "sub"  # unreachable while blocker is a file
    q.add(filename="a.pdf", destinations=[("folder", str(target))], pdf_data=PDF)
    assert q.run_due() == 0  # not due yet
    assert q.pending()[0]["attempts"] == 1
    clock.now += 60
    assert q.run_due() == 0
    e = q.pending()[0]
    assert e["attempts"] == 2
    raw = json.loads((tmp_path / "retry_queue.json").read_text())["entries"][0]
    assert raw["next_try_ts"] == clock.now + 300
    blocker.unlink()
    clock.now += 300
    assert q.run_due() == 1
    assert q.pending() == []
    assert (target / "a.pdf").read_bytes() == PDF
    # the queue's own copy is cleaned up
    assert list((tmp_path / "retry_files").iterdir()) == []


def test_retry_now_and_source_file_handling(tmp_path, clock):
    q = make(tmp_path, clock)
    src = tmp_path / "staged.pdf"
    src.write_bytes(PDF)
    out = tmp_path / "out"
    [eid] = q.add(filename="staged.pdf", destinations=[("folder", str(out))],
                  source_path=str(src), source_owned=True)
    assert q.retry_now("nope") is False
    assert q.retry_now(eid) is True
    assert (out / "staged.pdf").read_bytes() == PDF
    assert not src.exists()  # owned source removed after delivery


def test_unowned_source_is_kept(tmp_path, clock):
    q = make(tmp_path, clock)
    src = tmp_path / "watch.pdf"
    src.write_bytes(PDF)
    [eid] = q.add(filename="watch.pdf", destinations=[("folder", str(tmp_path / "o"))],
                  source_path=str(src), source_owned=False)
    assert q.retry_all() == 1
    assert src.exists()


def test_marked_failed_after_30_days_and_kept(tmp_path, clock):
    q = make(tmp_path, clock)
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    q.add(filename="a.pdf", destinations=[("folder", str(blocker / "x"))], pdf_data=PDF)
    clock.now += MAX_AGE + 1
    q.run_due()
    [e] = q.pending()
    assert e["status"] == "failed" and e["next_try"] is None
    assert len(list((tmp_path / "retry_files").iterdir())) == 1
    clock.now += 10 * DAILY
    assert q.run_due() == 0  # failed entries are not retried automatically


def test_start_stop_idempotent(tmp_path, clock):
    q = make(tmp_path, clock)
    q.stop()  # never started
    q.start(None)
    q.start(None)
    assert q.running()
    t0 = time.monotonic()
    q.stop()
    q.stop()
    assert not q.running()
    assert time.monotonic() - t0 < 5


def test_get_queue_uses_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENSCAN_CONFIG_DIR", str(tmp_path))
    retry_queue._reset_queue_for_tests()
    try:
        q = retry_queue.get_queue()
        assert q is retry_queue.get_queue()
        assert q.state_path == tmp_path / "retry_queue.json"
    finally:
        retry_queue._reset_queue_for_tests()


def test_queued_copy_gets_normal_file_mode(tmp_path, clock, monkeypatch):
    import os
    import stat
    if os.name == "nt":
        return
    from openscan import fileperm
    monkeypatch.setattr(fileperm, "_UMASK", 0o022)
    q = make(tmp_path, clock)
    q.add(filename="a.pdf", destinations=[("folder", str(tmp_path / "out"))], pdf_data=PDF)
    [entry] = q.pending()
    assert stat.S_IMODE(os.stat(entry["file"]).st_mode) == 0o644


def test_concurrent_retries_deliver_once(tmp_path, clock, monkeypatch):
    # A "Retry" click racing the background loop must not save two copies.
    import os
    import threading

    from openscan import output
    q = make(tmp_path, clock)
    out = tmp_path / "out"
    [eid] = q.add(filename="a.pdf", destinations=[("folder", str(out))], pdf_data=PDF)
    real = output.deliver_to
    started = threading.Event()
    release = threading.Event()

    def slow(*a, **k):
        started.set()
        release.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(output, "deliver_to", slow)
    first = threading.Thread(target=q.retry_now, args=(eid,))
    first.start()
    assert started.wait(5)
    # While the first attempt is in flight the entry is marked on disk ...
    on_disk = json.loads((tmp_path / "retry_queue.json").read_text())["entries"][0]
    assert on_disk.get("in_flight")
    # ... and a second attempt does nothing.
    assert q.retry_now(eid) is False
    clock.now += 10 * DAILY
    assert q.run_due() == 0
    release.set()
    first.join(5)
    assert sorted(os.listdir(out)) == ["a.pdf"]
    assert q.pending() == []


def test_in_flight_mark_is_cleared_after_a_crash(tmp_path, clock):
    q = make(tmp_path, clock)
    [eid] = q.add(filename="a.pdf", destinations=[("folder", str(tmp_path / "out"))], pdf_data=PDF)
    data = json.loads((tmp_path / "retry_queue.json").read_text())
    data["entries"][0]["in_flight"] = True
    (tmp_path / "retry_queue.json").write_text(json.dumps(data))
    q2 = make(tmp_path, clock)
    assert q2.retry_now(eid) is True


def test_restart_during_a_slow_attempt_leaves_one_loop(tmp_path, clock, monkeypatch):
    # stop() gives up waiting after STOP_JOIN seconds; the old loop must
    # still exit once its attempt ends instead of running beside the new one.
    import threading

    from openscan import output
    q = make(tmp_path, clock)
    monkeypatch.setattr(q, "STOP_JOIN", 0.1)
    q.add(filename="a.pdf", destinations=[("folder", str(tmp_path / "out"))], pdf_data=PDF)
    clock.now += DAILY
    started, release = threading.Event(), threading.Event()
    real = output.deliver_to

    def slow(*a, **k):
        started.set()
        release.wait(5)
        return real(*a, **k)

    monkeypatch.setattr(output, "deliver_to", slow)
    q.start(None)
    first = q._thread
    assert started.wait(5)
    q.stop()
    assert first.is_alive()  # still delivering
    q.start(None)
    release.set()
    first.join(5)
    try:
        assert not first.is_alive()
        loops = [t for t in threading.enumerate() if t.name == "retry-queue" and t.is_alive()]
        assert loops == [q._thread]
    finally:
        q.stop()
