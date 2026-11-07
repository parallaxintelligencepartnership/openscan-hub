"""Destination registry, edition filtering, delivery loop and retry compatibility."""

import json
import sys

import pytest

from openscan import _edition, destinations, retry_queue
from openscan.destinations import DeliveryError, Destination, register

PDF = b"%PDF-1.4 dest\n%%EOF"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    q = retry_queue.RetryQueue(tmp_path / "q" / "retry_queue.json", tmp_path / "q" / "files")
    monkeypatch.setattr(retry_queue, "_queue", q)
    yield q
    q.stop()


@pytest.fixture
def registry(monkeypatch):
    """A private copy of the registry, so test classes do not leak."""
    destinations._load()
    monkeypatch.setattr(destinations, "_registry", dict(destinations._registry))
    return destinations._registry


def test_builtin_types_and_fields():
    types = [cls.type for cls in destinations.available()]
    assert types[:3] == ["folder", "paperless_consume", "paperless_api"]
    for cls in destinations.available():
        assert cls.label
        for f in cls.FIELDS:
            assert set(f) == {"key", "label", "kind", "required", "default", "choices"}
            assert f["kind"] in destinations.FIELD_KINDS
    api = destinations.get("paperless_api")
    assert api.secret_keys() == {"token"}
    assert "token" not in api.public_options({"url": "u", "token": "t"})


def test_office_edition_hides_paperless(monkeypatch):
    monkeypatch.setattr(_edition, "EDITION", "office")
    assert [cls.type for cls in destinations.available()] == ["folder", "smb", "webdav", "email"]
    assert destinations.get("paperless_api") is None
    assert "paperless_api" in destinations.all_types()
    with pytest.raises(DeliveryError, match="not available"):
        destinations.deliver_one("paperless_api", PDF, "a.pdf", {})


def test_registry_survives_a_deleted_module(monkeypatch):
    # An Office build deletes paperless.py; the import fails and is skipped.
    monkeypatch.setattr(destinations, "_registry", {})
    monkeypatch.setattr(destinations, "_loaded", False)
    monkeypatch.setitem(sys.modules, "openscan.destinations.paperless", None)
    assert [cls.type for cls in destinations.available()] == ["folder", "smb", "webdav", "email"]
    monkeypatch.setattr(destinations, "_loaded", False)


def test_register_and_feature_filter(registry, monkeypatch):
    @register
    class Box(Destination):
        type = "box"
        label = "Box"
        feature = "audit_default_on"  # office only, for this test

        def deliver(self, pdf, filename, options):
            return f"box://{filename}", True

    assert destinations.get("box") is None
    monkeypatch.setattr(_edition, "EDITION", "office")
    assert destinations.get("box") is Box
    assert destinations.deliver_one("box", PDF, "a.pdf", {}) == ("box://a.pdf", True)


def test_folder_test_reports_problems(tmp_path):
    folder = destinations.get("folder")()
    assert folder.test({"folder": str(tmp_path)})["ok"] is True
    blocker = tmp_path / "file"
    blocker.write_text("x")
    res = folder.test({"folder": str(blocker / "sub")})
    assert res["ok"] is False and res["message"]


def test_deliver_all_queues_failures_without_secrets(tmp_path, queue):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    targets = [
        {"type": "folder", "options": {"folder": str(tmp_path / "out")}},
        {"type": "paperless_api", "options": {"url": "http://127.0.0.1:9", "token": "secret-t",
                                              "tags": ["Inbox"]}},
    ]
    res = destinations.deliver_all(PDF, "a.pdf", targets)
    assert res.ok is False and res.queued is True
    assert res.saved_paths == [str(tmp_path / "out" / "a.pdf")]
    raw = (tmp_path / "q" / "retry_queue.json").read_text()
    assert "secret-t" not in raw
    entry = json.loads(raw)["entries"][0]
    assert entry["destination"] == "paperless_api" and entry["target"] == "http://127.0.0.1:9"
    assert entry["options"]["tags"] == ["Inbox"]
    assert entry["paperless"]["tags"] == ["Inbox"]  # read by older versions


def test_retry_reads_pre_04_entries(tmp_path, queue):
    out = tmp_path / "out"
    src = tmp_path / "old.pdf"
    src.write_bytes(PDF)
    state = {"version": 1, "entries": [{
        "id": "abc123", "file": str(src), "filename": "old.pdf", "destination": "folder",
        "target": str(out), "owns_file": False, "paperless": {}, "attempts": 1,
        "last_error": "x", "created": 0, "next_try_ts": 0, "status": "pending"}]}
    queue.state_path.parent.mkdir(parents=True, exist_ok=True)
    queue.state_path.write_text(json.dumps(state))
    queue._load()
    assert queue.retry_now("abc123") is True
    assert (out / "old.pdf").read_bytes() == PDF


def test_retry_fills_the_token_from_config():
    from openscan.config import AppConfig
    cfg = AppConfig()
    cfg.paperless.api_token = "from-settings"
    entry = {"destination": "paperless_api", "target": "http://pl", "paperless": {"tags": ["A"]}}
    opts = retry_queue.entry_options(entry, cfg)
    assert opts["token"] == "from-settings" and opts["url"] == "http://pl" and opts["tags"] == ["A"]
