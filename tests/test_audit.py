"""Audit log: append, recent, rotation, threads, config default."""

import json
import threading

from openscan import audit


def test_record_and_recent(tmp_path):
    path = tmp_path / "audit.jsonl"
    assert audit.record({"source": "adf", "pages": 2}, path)
    audit.record({"source": "ftp", "time": "fixed"}, path)
    lines = path.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["time"]
    assert [e["source"] for e in audit.recent(10, path)] == ["ftp", "adf"]
    assert audit.recent(1, path)[0]["time"] == "fixed"


def test_rotation_keeps_five_files(tmp_path, monkeypatch):
    monkeypatch.setattr(audit, "MAX_BYTES", 200)
    path = tmp_path / "audit.jsonl"
    for i in range(60):
        audit.record({"i": i, "pad": "x" * 40}, path)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["audit.jsonl"] + [f"audit.jsonl.{i}" for i in range(1, 6)]
    assert all(p.stat().st_size <= 200 for p in tmp_path.iterdir())
    events = audit.recent(1000, path)
    assert events[0]["i"] == 59
    assert [e["i"] for e in events] == sorted((e["i"] for e in events), reverse=True)


def test_threads_write_whole_lines(tmp_path):
    path = tmp_path / "audit.jsonl"
    threads = [threading.Thread(target=lambda k=k: [audit.record({"k": k, "j": j}, path)
                                                    for j in range(50)]) for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = path.read_text().splitlines()
    assert len(lines) == 400 and all(json.loads(line) for line in lines)


def test_default_path_is_in_the_config_dir(config_dir):
    audit.record({"x": 1})
    assert (config_dir / "audit.jsonl").exists()


def test_write_failure_is_not_an_error(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    assert audit.record({"x": 1}, blocker / "audit.jsonl") is False
