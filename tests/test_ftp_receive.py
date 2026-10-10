import ftplib
import io
import time
from pathlib import Path

import pytest

from openscan import retry_queue
from openscan.config import AppConfig
from openscan.scanner import ftp_receive
from openscan.scanner.ftp_receive import (
    ftp_receiver_address,
    ftp_receiver_running,
    parse_passive_ports,
    sanitize_filename,
    start_ftp_receiver,
    stop_ftp_receiver,
)

PDF = b"%PDF-1.4 ftp scan\n%%EOF"
USER, PASSWORD = "scan", "s3cret-Pass-123"


@pytest.fixture
def queue(tmp_path, monkeypatch):
    q = retry_queue.RetryQueue(tmp_path / "q" / "retry_queue.json", tmp_path / "q" / "files")
    monkeypatch.setattr(retry_queue, "_queue", q)
    yield q
    q.stop()


def make_config(tmp_path, **ftp):
    cfg = AppConfig()
    cfg.output.folder = str(tmp_path / "out")
    cfg.ftp_receive.enabled = True
    cfg.ftp_receive.port = 0
    cfg.ftp_receive.username = USER
    cfg.ftp_receive.password = PASSWORD
    cfg.ftp_receive.staging_dir = str(tmp_path / "staging")
    cfg.ftp_receive.passive_ports = "61000-61020"
    for k, v in ftp.items():
        setattr(cfg.ftp_receive, k, v)
    return cfg


@pytest.fixture
def server(tmp_path, queue):
    started = []

    def _start(**ftp):
        cfg = make_config(tmp_path, **ftp)
        assert start_ftp_receiver(cfg, host="127.0.0.1")
        started.append(True)
        return cfg, ftp_receiver_address()[1]

    yield _start
    stop_ftp_receiver()


def connect(port, user=USER, password=PASSWORD):
    ftp = ftplib.FTP()
    ftp.connect("127.0.0.1", port, timeout=5)
    ftp.login(user, password)
    return ftp


def wait_for(pred, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred():
            return True
        time.sleep(0.05)
    return False


def test_anonymous_refused_by_default(server):
    _, port = server()
    with pytest.raises(ftplib.error_perm):
        connect(port, "anonymous", "x@y")


def test_anonymous_allowed_only_when_enabled(server, tmp_path):
    _, port = server(allow_anonymous=True)
    ftp = connect(port, "anonymous", "x@y")
    ftp.storbinary("STOR anon.pdf", io.BytesIO(PDF))
    ftp.quit()
    assert wait_for(lambda: (tmp_path / "out" / "anon.pdf").exists())


def test_wrong_password_refused(server):
    _, port = server()
    with pytest.raises(ftplib.error_perm):
        connect(port, USER, "wrong")


def test_upload_routed_and_staging_cleaned(server, tmp_path):
    _, port = server()
    assert ftp_receiver_running()
    ftp = connect(port)
    ftp.storbinary("STOR doc.pdf", io.BytesIO(PDF))
    ftp.quit()
    out = tmp_path / "out" / "doc.pdf"
    assert wait_for(out.exists)
    assert out.read_bytes() == PDF
    assert wait_for(lambda: not any((tmp_path / "staging" / "received").iterdir()))


def test_delete_and_download_refused(server, tmp_path):
    cfg, port = server(delete_after_routing=False)
    ftp = connect(port)
    ftp.storbinary("STOR keep.pdf", io.BytesIO(PDF))
    assert wait_for(lambda: (tmp_path / "out" / "keep.pdf").exists())
    ftp.cwd("received")
    assert "keep.pdf" in ftp.nlst()
    with pytest.raises(ftplib.error_perm):
        ftp.delete("keep.pdf")
    with pytest.raises(ftplib.error_perm):
        ftp.retrbinary("RETR keep.pdf", lambda b: None)
    with pytest.raises(ftplib.error_perm):
        ftp.rename("keep.pdf", "other.pdf")
    ftp.quit()
    assert (tmp_path / "staging" / "received" / "keep.pdf").exists()


def test_failure_keeps_staged_file_and_queues(server, tmp_path, queue):
    blocker = tmp_path / "out"
    blocker.write_text("not a folder")  # output folder cannot be created
    _, port = server()
    ftp = connect(port)
    ftp.storbinary("STOR lost.pdf", io.BytesIO(PDF))
    ftp.quit()
    assert wait_for(lambda: len(queue.pending()) == 1)
    staged = tmp_path / "staging" / "received" / "lost.pdf"
    time.sleep(0.2)
    assert staged.read_bytes() == PDF
    [entry] = queue.pending()
    assert entry["file"] == str(staged)


def test_same_name_uploads_do_not_overwrite(server, tmp_path):
    _, port = server(delete_after_routing=False)
    ftp = connect(port)
    ftp.storbinary("STOR same.pdf", io.BytesIO(PDF + b"1"))
    ftp.storbinary("STOR same.pdf", io.BytesIO(PDF + b"2"))
    ftp.quit()
    out = tmp_path / "out"
    assert wait_for(lambda: len(list(out.glob("same*.pdf"))) == 2)
    assert sorted(p.read_bytes() for p in out.glob("same*.pdf")) == [PDF + b"1", PDF + b"2"]


def test_start_without_credentials_refused(tmp_path, queue):
    cfg = make_config(tmp_path, password="")
    assert start_ftp_receiver(cfg, host="127.0.0.1") is False
    assert "username and password" in ftp_receive.ftp_receiver_error()
    assert not ftp_receiver_running()


def test_start_stop_idempotent(tmp_path, queue):
    stop_ftp_receiver()  # never started
    cfg = make_config(tmp_path)
    assert start_ftp_receiver(cfg, host="127.0.0.1")
    assert start_ftp_receiver(cfg, host="127.0.0.1")
    t0 = time.monotonic()
    stop_ftp_receiver()
    stop_ftp_receiver()
    assert time.monotonic() - t0 < 5
    assert not ftp_receiver_running()


def test_sanitize_filename():
    assert sanitize_filename("../../etc/passwd") == "passwd"
    assert sanitize_filename("C:\\x\\a\x00b\x1f.pdf") == "ab.pdf"
    assert sanitize_filename('a"b<c>.pdf') == "a_b_c_.pdf"
    assert sanitize_filename("") == "scan"
    assert sanitize_filename(".pdf") == "scan.pdf"


def test_parse_passive_ports():
    assert parse_passive_ports("60000-60010") == range(60000, 60011)
    assert parse_passive_ports("bad") is None
    assert parse_passive_ports("10-20") is None


def test_passive_reply_uses_the_configured_address(server):
    # In Docker bridge mode the container IP is useless to the scanner; the
    # PASV reply must carry the address the scanner can reach.
    _, port = server(passive_address="192.0.2.77")
    ftp = connect(port)
    try:
        reply = ftp.sendcmd("PASV")
    finally:
        ftp.close()
    assert "(192,0,2,77," in reply


def test_passive_address_auto_and_fallback(monkeypatch):
    cfg = AppConfig()
    cfg.ftp_receive.passive_address = "198.51.100.4"
    assert ftp_receive.passive_address(cfg, "0.0.0.0") == "198.51.100.4"
    cfg.ftp_receive.passive_address = ""
    monkeypatch.setattr(ftp_receive, "primary_ipv4", lambda: "192.168.1.20")
    assert ftp_receive.passive_address(cfg, "0.0.0.0") == "192.168.1.20"
    # Bound to one address: data connections can only go there.
    assert ftp_receive.passive_address(cfg, "10.0.0.5") == "10.0.0.5"
    monkeypatch.setattr(ftp_receive, "primary_ipv4", lambda: "")
    assert ftp_receive.passive_address(cfg, "0.0.0.0") == ""
    cfg.ftp_receive.passive_address = "nas.lan"  # not an IPv4 address: ignored
    assert ftp_receive.passive_address(cfg, "0.0.0.0") == ""


def test_passive_address_env_override(tmp_path, monkeypatch):
    from openscan.config import load_config
    monkeypatch.setenv("OPENSCAN_FTP_PASSIVE_ADDRESS", "192.168.1.9")
    assert load_config(tmp_path / "c.json").ftp_receive.passive_address == "192.168.1.9"
