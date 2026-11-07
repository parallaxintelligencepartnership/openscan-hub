"""Mail receiver. The IMAP tests run imaplib against tests/fake_imap.py, a
real IMAP4rev1 server over TCP with real TLS and STARTTLS (a throwaway
certificate made with the openssl program). No mocks of imaplib."""

import shutil
import ssl
import subprocess
import time
from email.message import EmailMessage

import pytest

from openscan import retry_queue
from openscan.config import AppConfig
from openscan.scanner import imap_receive
from openscan.scanner.imap_receive import extract_attachments, sender_allowed

from fake_imap import FakeImapServer

PDF = b"%PDF-1.4 mail scan\n%%EOF"
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 20
PASSWORD = "mail-pw-123"


def make_mail(sender="Scanner <scanner@example.com>", attachments=(("scan.pdf", "application/pdf", PDF),),
              subject="Scan"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "scan@example.net"
    msg["Subject"] = subject
    msg.set_content("Your scan is attached.")
    for name, ctype, data in attachments:
        main, _, sub = ctype.partition("/")
        msg.add_attachment(data, maintype=main, subtype=sub, filename=name)
    return msg.as_bytes()


@pytest.fixture(scope="module")
def tls(tmp_path_factory):
    openssl = shutil.which("openssl")
    if not openssl:
        pytest.skip("openssl is not installed")
    d = tmp_path_factory.mktemp("tls")
    subprocess.run([openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2",
                    "-keyout", str(d / "key.pem"), "-out", str(d / "cert.pem"), "-subj", "/CN=localhost",
                    "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1"],
                   check=True, capture_output=True)
    server_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_ctx.load_cert_chain(str(d / "cert.pem"), str(d / "key.pem"))
    return {"server": server_ctx, "cert": str(d / "cert.pem")}


@pytest.fixture
def queue(tmp_path, monkeypatch):
    q = retry_queue.RetryQueue(tmp_path / "q" / "retry_queue.json", tmp_path / "q" / "files")
    monkeypatch.setattr(retry_queue, "_queue", q)
    yield q
    q.stop()


@pytest.fixture
def trust(tls, monkeypatch):
    """Clients trust the throwaway certificate (and still check it)."""
    monkeypatch.setattr(imap_receive, "_ssl_context",
                        lambda: ssl.create_default_context(cafile=tls["cert"]))


def config_for(server, tmp_path, security="ssl", **kw):
    cfg = AppConfig()
    cfg.wizard_completed = True
    cfg.output.folder = str(tmp_path / "out")
    c = cfg.imap_receive
    c.enabled = True
    c.host = "localhost"
    c.port = server.port
    c.security = security
    c.username = "scan"
    c.password = PASSWORD
    c.poll_interval = 0.1
    for k, v in kw.items():
        setattr(c, k, v)
    return cfg


@pytest.fixture
def ssl_server(tls):
    s = FakeImapServer(ssl_context=tls["server"], implicit_tls=True)
    yield s
    s.close()


@pytest.fixture
def starttls_server(tls):
    s = FakeImapServer(ssl_context=tls["server"])
    yield s
    s.close()


def out_files(tmp_path):
    d = tmp_path / "out"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def test_ssl_delivers_pdf_marks_seen_and_moves(ssl_server, trust, tmp_path, queue):
    ssl_server.add(make_mail())
    cfg = config_for(ssl_server, tmp_path)
    assert imap_receive.poll_once(cfg) == 1
    assert (tmp_path / "out" / "scan.pdf").read_bytes() == PDF
    assert ssl_server.messages("INBOX") == []  # moved out
    [moved] = ssl_server.messages("Processed")
    assert "\\Seen" in moved["flags"]
    # Nothing left: a second poll delivers nothing.
    assert imap_receive.poll_once(cfg) == 0
    assert out_files(tmp_path) == ["scan.pdf"]


def test_starttls_works(starttls_server, trust, tmp_path, queue):
    starttls_server.add(make_mail())
    cfg = config_for(starttls_server, tmp_path, security="starttls")
    assert imap_receive.poll_once(cfg) == 1
    assert out_files(tmp_path) == ["scan.pdf"]
    assert any(line.startswith("STARTTLS") for line in starttls_server.log)


def test_untrusted_certificate_is_refused(ssl_server, tmp_path, queue):
    """With the default context the throwaway certificate must be rejected."""
    ssl_server.add(make_mail())
    cfg = config_for(ssl_server, tmp_path)
    assert imap_receive.poll_once(cfg) == 0
    assert "CERTIFICATE" in imap_receive.imap_receive_last_error().upper()
    assert out_files(tmp_path) == []
    assert not any(line.startswith("LOGIN") for line in ssl_server.log)  # no password sent


def test_password_never_logged_or_reported(ssl_server, trust, tmp_path, queue, caplog):
    cfg = config_for(ssl_server, tmp_path, password="wrong-pw-999")
    with caplog.at_level("DEBUG"):
        assert imap_receive.poll_once(cfg) == 0
    assert "wrong-pw-999" not in caplog.text
    assert "wrong-pw-999" not in (imap_receive.imap_receive_last_error() or "")
    assert imap_receive.imap_receive_last_error()


def test_allowed_senders_filter(ssl_server, trust, tmp_path, queue):
    ssl_server.add(make_mail(sender="Spam <evil@bad.example>"))
    ssl_server.add(make_mail(sender="boss@good.example", attachments=(("ok.pdf", "application/pdf", PDF),)))
    cfg = config_for(ssl_server, tmp_path, allowed_senders=["@good.example"])
    assert imap_receive.poll_once(cfg) == 1
    assert out_files(tmp_path) == ["ok.pdf"]
    [left] = ssl_server.messages("INBOX")  # the stranger's mail stays unseen
    assert "\\Seen" not in left["flags"]


def test_pdf_and_image_attachments_and_other_types_ignored(ssl_server, trust, tmp_path, queue):
    ssl_server.add(make_mail(attachments=(
        ("one.pdf", "application/pdf", PDF),
        ("two.png", "image/png", PNG),
        ("notes.txt", "text/plain", b"hello"),
        ("macro.docx", "application/octet-stream", b"PK"),
    )))
    cfg = config_for(ssl_server, tmp_path)
    assert imap_receive.poll_once(cfg) == 1
    assert out_files(tmp_path) == ["one.pdf", "two.pdf"] or out_files(tmp_path) == ["one.pdf", "two.png"]


def test_mail_without_attachment_is_left_alone(ssl_server, trust, tmp_path, queue):
    ssl_server.add(make_mail(attachments=()))
    cfg = config_for(ssl_server, tmp_path)
    skipped = set()
    assert imap_receive.poll_once(cfg, skipped) == 0
    assert len(skipped) == 1
    [m] = ssl_server.messages("INBOX")
    assert "\\Seen" not in m["flags"]


def test_no_move_folder_only_marks_seen(ssl_server, trust, tmp_path, queue):
    ssl_server.add(make_mail())
    cfg = config_for(ssl_server, tmp_path, move_to_folder="")
    assert imap_receive.poll_once(cfg) == 1
    [m] = ssl_server.messages("INBOX")
    assert "\\Seen" in m["flags"]
    assert "Processed" not in ssl_server.folders


def test_failed_delivery_leaves_mail_unseen_for_retry(ssl_server, trust, tmp_path, monkeypatch):
    from openscan import intake

    class Fail:
        ok, queued, errors, filename, size_bytes = False, False, ["disk full"], "", 0

        def to_dict(self):
            return {"ok": False, "saved_paths": [], "errors": self.errors, "queued": False}

    ssl_server.add(make_mail())
    cfg = config_for(ssl_server, tmp_path)
    real = intake.handle_scan
    monkeypatch.setattr(intake, "handle_scan", lambda *a, **k: Fail())
    assert imap_receive.poll_once(cfg) == 0
    [m] = ssl_server.messages("INBOX")
    assert "\\Seen" not in m["flags"]
    monkeypatch.setattr(intake, "handle_scan", real)
    q = retry_queue.RetryQueue(tmp_path / "q" / "r.json", tmp_path / "q" / "f")
    monkeypatch.setattr(retry_queue, "_queue", q)
    assert imap_receive.poll_once(cfg) == 1
    assert out_files(tmp_path) == ["scan.pdf"]
    q.stop()


def test_service_loop_start_stop(ssl_server, trust, tmp_path, queue):
    cfg = config_for(ssl_server, tmp_path)
    assert imap_receive.start_imap_receive(cfg)
    assert imap_receive.imap_receive_running()
    ssl_server.add(make_mail())
    end = time.monotonic() + 10
    while time.monotonic() < end and not out_files(tmp_path):
        time.sleep(0.1)
    imap_receive.stop_imap_receive()
    assert out_files(tmp_path) == ["scan.pdf"]
    assert not imap_receive.imap_receive_running()


# --- helpers that need no server ---------------------------------------------

def test_start_refuses_missing_settings():
    cfg = AppConfig()
    assert imap_receive.start_imap_receive(cfg) is False
    assert "server" in imap_receive.imap_receive_error()
    cfg.imap_receive.host = "mail.example.com"
    assert imap_receive.start_imap_receive(cfg) is False
    assert "user" in imap_receive.imap_receive_error()


def test_sender_allowed_rules():
    assert sender_allowed("anyone@x.example", [])
    assert sender_allowed("Bob <Bob@Good.Example>", ["bob@good.example"])
    assert sender_allowed("bob@good.example", ["@good.example"])
    assert sender_allowed("bob@good.example", ["good.example"])
    assert not sender_allowed("bob@evilgood.example", ["good.example"])
    assert not sender_allowed("alice@good.example", ["bob@good.example"])
    assert not sender_allowed("", ["good.example"])


def test_inline_logo_is_not_taken():
    msg = EmailMessage()
    msg["From"] = "a@b.example"
    msg.set_content("hi")
    msg.add_attachment(PNG, maintype="image", subtype="png")  # no file name
    import email
    import email.policy
    parsed = email.message_from_bytes(msg.as_bytes(), policy=email.policy.default)
    # An attachment without a name is still an attachment and is taken.
    [(name, data)] = extract_attachments(parsed)
    assert data == PNG and name.startswith("attachment")


def test_mail_stays_unseen_when_one_document_is_neither_delivered_nor_queued(
        ssl_server, trust, tmp_path, monkeypatch, queue):
    from openscan import intake
    from test_intake import partial_result
    ssl_server.add(make_mail())
    cfg = config_for(ssl_server, tmp_path)
    calls = []
    monkeypatch.setattr(intake, "handle_scan", lambda *a, **k: calls.append(1) or partial_result())
    assert imap_receive.poll_once(cfg) == 0
    assert calls == [1]
    [m] = ssl_server.messages("INBOX")
    assert "\\Seen" not in m["flags"]
