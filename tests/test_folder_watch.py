import time

import pytest

from openscan import retry_queue
from openscan.config import AppConfig
from openscan.scanner import folder_watch
from openscan.scanner.folder_watch import (
    folder_watcher_error,
    folder_watcher_running,
    start_folder_watcher,
    stop_folder_watcher,
)

PDF = b"%PDF-1.4 watched\n%%EOF"


@pytest.fixture(autouse=True)
def _cleanup(tmp_path, monkeypatch):
    q = retry_queue.RetryQueue(tmp_path / "q" / "retry_queue.json", tmp_path / "q" / "files")
    monkeypatch.setattr(retry_queue, "_queue", q)
    yield
    stop_folder_watcher()
    q.stop()


def cfg_for(tmp_path, watch, out, consume=None):
    cfg = AppConfig()
    cfg.folder_watch.enabled = True
    cfg.folder_watch.watch_folder = str(watch)
    cfg.folder_watch.poll_interval = 0.05
    cfg.output.folder = str(out)
    if consume is not None:
        cfg.paperless.enabled = True
        cfg.paperless.mode = "consume"
        cfg.paperless.consume_folder = str(consume)
    return cfg


@pytest.mark.parametrize("watch_rel,out_rel", [
    ("Scans", "Scans"),            # equal
    ("Scans", "Scans/out"),        # watch contains output
    ("Scans/in", "Scans"),         # watch inside output
    ("Scans/../Scans", "Scans"),   # equal after resolving
])
def test_refuses_overlap_with_output(tmp_path, watch_rel, out_rel):
    cfg = cfg_for(tmp_path, tmp_path / watch_rel, tmp_path / out_rel)
    assert start_folder_watcher(cfg) is False
    assert not folder_watcher_running()
    assert "scans folder" in folder_watcher_error()


def test_refuses_overlap_with_consume_folder(tmp_path):
    cfg = cfg_for(tmp_path, tmp_path / "consume" / "in", tmp_path / "out", consume=tmp_path / "consume")
    assert start_folder_watcher(cfg) is False
    assert "consume folder" in folder_watcher_error()


def test_consume_folder_ignored_when_paperless_disabled(tmp_path):
    cfg = cfg_for(tmp_path, tmp_path / "c", tmp_path / "out", consume=tmp_path / "c")
    cfg.paperless.enabled = False
    assert start_folder_watcher(cfg) is True
    assert folder_watcher_error() is None


def test_refuses_empty_watch_folder(tmp_path):
    cfg = cfg_for(tmp_path, "", tmp_path / "out")
    assert start_folder_watcher(cfg) is False


def test_sibling_folders_with_shared_prefix_allowed(tmp_path):
    cfg = cfg_for(tmp_path, tmp_path / "Scans-in", tmp_path / "Scans")
    assert start_folder_watcher(cfg) is True


def test_routes_new_file_once_and_stops(tmp_path):
    watch, out = tmp_path / "in", tmp_path / "out"
    watch.mkdir()
    (watch / "old.pdf").write_bytes(PDF)
    cfg = cfg_for(tmp_path, watch, out)
    assert start_folder_watcher(cfg) is True
    assert start_folder_watcher(cfg) is True  # idempotent
    assert folder_watcher_running()
    (watch / "new.pdf").write_bytes(PDF)
    (watch / ".hidden.pdf").write_bytes(PDF)
    end = time.monotonic() + 5
    while time.monotonic() < end and not (out / "new.pdf").exists():
        time.sleep(0.05)
    time.sleep(0.3)
    assert sorted(p.name for p in out.iterdir()) == ["new.pdf"]
    assert (watch / "new.pdf").exists()
    t0 = time.monotonic()
    stop_folder_watcher()
    stop_folder_watcher()
    assert time.monotonic() - t0 < 5
    assert not folder_watcher_running()


def test_stop_without_start():
    stop_folder_watcher()
    assert not folder_watcher_running()
