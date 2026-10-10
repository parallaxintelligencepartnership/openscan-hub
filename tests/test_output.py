import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from openscan import output, retry_queue
from openscan.output import DeliveryResult, generate_filename, save_scan, save_to_folder

PDF = b"%PDF-1.4 test scan\n%%EOF"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    q = retry_queue.RetryQueue(tmp_path / "q" / "retry_queue.json", tmp_path / "q" / "files")
    monkeypatch.setattr(retry_queue, "_queue", q)
    yield q
    q.stop()


@pytest.fixture(autouse=True)
def _clear_id_cache():
    output._paperless_ids.clear()
    yield
    output._paperless_ids.clear()


class FakePaperless:
    """Minimal Paperless-NGX API on an ephemeral port."""

    def __init__(self, task_status="SUCCESS", upload_status=200):
        self.task_status = task_status
        self.upload_status = upload_status
        self.tags = {"inbox": 1}
        self.correspondents = {"acme": 7}
        self.document_types = {}
        self.uploads = []
        self.created_tags = []
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, obj):
                body = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                from urllib.parse import parse_qs, urlparse
                u = urlparse(self.path)
                q = parse_qs(u.query)
                for kind in ("tags", "correspondents", "document_types"):
                    if u.path == f"/api/{kind}/":
                        name = q.get("name__iexact", [""])[0]
                        table = getattr(fake, kind)
                        res = [{"id": i, "name": n} for n, i in table.items() if n.lower() == name.lower()]
                        return self._json(200, {"count": len(res), "results": res})
                if u.path == "/api/tasks/":
                    return self._json(200, [{"task_id": q["task_id"][0], "status": fake.task_status,
                                             "result": "It is a duplicate", "related_document": 42}])
                self._json(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n)
                if self.path == "/api/tags/":
                    data = json.loads(body)
                    new_id = 100 + len(fake.created_tags)
                    fake.created_tags.append(data)
                    fake.tags[data["name"]] = new_id
                    return self._json(201, {"id": new_id, "name": data["name"]})
                if self.path == "/api/documents/post_document/":
                    if fake.upload_status != 200:
                        return self._json(fake.upload_status, {"error": "nope"})
                    fake.uploads.append(body)
                    return self._json(200, "task-123")
                self._json(404, {})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def paperless():
    fp = FakePaperless()
    yield fp
    fp.close()


def test_folder_only_success(tmp_path, queue):
    out = tmp_path / "scans" / "nested"
    res = save_scan(PDF, str(out), "doc.pdf")
    assert isinstance(res, DeliveryResult)
    assert res.ok and not res.errors and not res.queued
    assert res.saved_paths == [str(out / "doc.pdf")]
    assert (out / "doc.pdf").read_bytes() == PDF
    assert queue.pending() == []


def test_atomic_write_leaves_no_temp_files(tmp_path, queue):
    save_to_folder(PDF, str(tmp_path), "a.pdf")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.pdf"]


def test_atomic_write_failure_leaves_nothing(tmp_path, monkeypatch):
    def boom(src, dst):
        raise OSError(5, "Input/output error")
    monkeypatch.setattr(output.os, "replace", boom)
    with pytest.raises(output.DeliveryError):
        save_to_folder(PDF, str(tmp_path), "a.pdf")
    assert list(tmp_path.iterdir()) == []


def test_collision_counter(tmp_path):
    name = generate_filename("scan_{n}")
    assert name == "scan_001.pdf"
    paths = [save_to_folder(PDF, str(tmp_path), name) for _ in range(3)]
    assert [Path(p).name for p in paths] == ["scan_001.pdf", "scan_002.pdf", "scan_003.pdf"]
    assert generate_filename("scan_{n}", folder=str(tmp_path)) == "scan_004.pdf"
    other = [Path(save_to_folder(PDF, str(tmp_path), "x.pdf")).name for _ in range(2)]
    assert other == ["x.pdf", "x_2.pdf"]


def test_collision_counter_concurrent(tmp_path):
    results = []
    threads = [threading.Thread(target=lambda: results.append(save_to_folder(PDF, str(tmp_path), "c_001.pdf")))
               for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(results)) == 10
    assert len([p for p in tmp_path.iterdir() if p.suffix == ".pdf"]) == 10


def test_unreachable_share_message():
    msg = output.folder_error_message(r"\\nas\scans", FileNotFoundError(2, "No such file"))
    assert msg == r"Could not reach \\nas\scans. Check that the share is online and the path is correct."


def test_unwritable_folder_queues_and_keeps_bytes(tmp_path, queue):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    res = save_scan(PDF, str(blocker / "sub"), "doc.pdf")
    assert res.ok is False and res.queued is True
    assert res.errors and "Could not save" in res.errors[0]
    [entry] = queue.pending()
    assert entry["destination"] == "folder"
    assert Path(entry["file"]).read_bytes() == PDF


def test_failing_paperless_keeps_file_and_queues(tmp_path, queue):
    out = tmp_path / "scans"
    src = tmp_path / "staging" / "doc.pdf"
    src.parent.mkdir()
    src.write_bytes(PDF)
    res = save_scan(PDF, str(out), "doc.pdf", paperless_api_url="http://127.0.0.1:9",
                    paperless_api_token="t", source_path=str(src), source_owned=True)
    assert res.ok is False and res.queued is True
    assert res.saved_paths == [str(out / "doc.pdf")]
    assert "Could not reach Paperless-NGX" in res.errors[0]
    assert src.exists()
    [entry] = queue.pending()
    assert entry["destination"] == "paperless_api" and entry["file"] == str(src)


def test_no_destination_is_an_error(queue):
    res = save_scan(PDF, "", "doc.pdf")
    assert res.ok is False and res.errors


def test_paperless_disabled_does_nothing(tmp_path, queue):
    from openscan.config import AppConfig
    cfg = AppConfig()
    cfg.output.folder = str(tmp_path)
    cfg.paperless.enabled = False
    cfg.paperless.api_url = "http://127.0.0.1:9"
    cfg.paperless.api_token = "t"
    cfg.paperless.mode = "api"
    res = output.save_scan_for_config(cfg, PDF, "doc.pdf")
    assert res.ok and res.saved_paths == [str(tmp_path / "doc.pdf")]


def test_paperless_api_resolves_names_and_confirms(tmp_path, queue, paperless):
    res = save_scan(PDF, str(tmp_path), 'we"ird.pdf', paperless_api_url=paperless.url,
                    paperless_api_token="t", paperless_tags=["Inbox", "Receipts"],
                    paperless_correspondent="ACME", paperless_document_type="Missing",
                    keep_local_copy=False)
    assert res.ok, res.errors
    # local copy removed only because Paperless confirmed
    assert not (tmp_path / 'we"ird.pdf').exists()
    assert res.saved_paths == [f"{paperless.url}/documents/42/details"]
    body = paperless.uploads[0]
    assert b'name="tags"\r\n\r\n1\r\n' in body
    assert b'name="tags"\r\n\r\n100\r\n' in body
    assert b'name="correspondent"\r\n\r\n7\r\n' in body
    assert b"document_type" not in body
    assert b'filename="we_ird.pdf"' in body
    assert paperless.created_tags == [{"name": "Receipts", "matching_algorithm": 0}]
    # ids are cached: a second upload creates nothing new
    save_scan(PDF, "", "b.pdf", paperless_api_url=paperless.url, paperless_api_token="t",
              paperless_tags=["Receipts"])
    assert len(paperless.created_tags) == 1


def test_paperless_task_failure_keeps_local_copy(tmp_path, queue, paperless):
    paperless.task_status = "FAILURE"
    res = save_scan(PDF, str(tmp_path), "d.pdf", paperless_api_url=paperless.url,
                    paperless_api_token="t", keep_local_copy=False)
    assert res.ok is False and res.queued is True
    assert "could not process" in res.errors[0]
    assert (tmp_path / "d.pdf").exists()


def test_paperless_unconfirmed_keeps_local_copy(tmp_path, queue, paperless):
    paperless.task_status = "PENDING"
    res = save_scan(PDF, str(tmp_path), "d.pdf", paperless_api_url=paperless.url,
                    paperless_api_token="t", keep_local_copy=False, task_wait=0.3)
    assert res.ok is True
    assert (tmp_path / "d.pdf").exists()


def test_paperless_bad_token_message(tmp_path, queue, paperless):
    paperless.upload_status = 401
    res = save_scan(PDF, "", "d.pdf", paperless_api_url=paperless.url, paperless_api_token="bad")
    assert res.ok is False
    assert "API token" in res.errors[0]


def test_delivery_result_serializes():
    r = DeliveryResult(ok=True, saved_paths=["a"])
    assert json.loads(json.dumps(r.to_dict())) == {"ok": True, "saved_paths": ["a"], "errors": [], "queued": False}



@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_saved_scan_gets_normal_file_mode(tmp_path, monkeypatch):
    import stat

    from openscan import fileperm
    monkeypatch.setattr(fileperm, "_UMASK", 0o022)
    path = save_to_folder(b"%PDF-1.4\n", str(tmp_path), "scan.pdf")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o644
    monkeypatch.setattr(fileperm, "_UMASK", 0o002)
    path = save_to_folder(b"%PDF-1.4\n", str(tmp_path), "scan.pdf")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o664


def test_file_mode_fix_is_best_effort(tmp_path, monkeypatch):
    from openscan import fileperm

    def refuse(fd, mode):
        raise PermissionError("CIFS says no")
    monkeypatch.setattr(fileperm.os, "fchmod", refuse, raising=False)
    assert save_to_folder(b"%PDF-1.4\n", str(tmp_path), "scan.pdf")
    monkeypatch.delattr(fileperm.os, "fchmod", raising=False)
    assert save_to_folder(b"%PDF-1.4\n", str(tmp_path), "scan.pdf")
