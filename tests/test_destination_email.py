"""Email destination against a small SMTP sink started inside the test."""

import base64
import logging
import socketserver
import threading

import pytest

from openscan.destinations import DeliveryError
from openscan.destinations.email import EmailDestination

PDF = b"%PDF-1.4 mail\n%%EOF"
PASSWORD = "s3cret-pw-value"


class _Sink(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, user="bob", password=PASSWORD):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.user, self.password = user, password
        self.messages = []
        self.logins = 0

    @property
    def port(self):
        return self.server_address[1]


class _Handler(socketserver.StreamRequestHandler):
    def send(self, line):
        self.wfile.write(line.encode() + b"\r\n")

    def handle(self):
        srv = self.server
        self.send("220 sink ready")
        authed = False
        while True:
            raw = self.rfile.readline()
            if not raw:
                return
            line = raw.decode(errors="replace").strip()
            cmd = line.split(" ", 1)[0].upper()
            if cmd == "EHLO":
                self.wfile.write(b"250-sink\r\n250-AUTH PLAIN\r\n250 SIZE 99999999\r\n")
            elif cmd == "AUTH":
                _, mech, blob = (line.split(" ") + ["", ""])[:3]
                _, user, pw = base64.b64decode(blob).decode().split("\0")
                if (user, pw) == (srv.user, srv.password):
                    authed = True
                    srv.logins += 1
                    self.send("235 ok")
                else:
                    self.send("535 5.7.8 bad credentials")
            elif cmd in ("MAIL", "RCPT", "RSET", "NOOP"):
                self.send("250 ok")
            elif cmd == "DATA":
                self.send("354 go")
                data = []
                while True:
                    row = self.rfile.readline()
                    if row in (b".\r\n", b""):
                        break
                    data.append(row)
                srv.messages.append((authed, b"".join(data)))
                self.send("250 queued")
            elif cmd == "QUIT":
                self.send("221 bye")
                return
            else:
                self.send("502 no")


@pytest.fixture
def sink():
    s = _Sink()
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    yield s
    s.shutdown()
    s.server_close()


def opts(sink, **kw):
    base = {"host": "127.0.0.1", "port": str(sink.port), "security": "none",
            "username": "bob", "password": PASSWORD, "from": "scans@example.com",
            "to": ["a@example.com", "b@example.com"], "subject": "Scan {filename}",
            "max_attachment_mb": "1"}
    base.update(kw)
    return base


def test_fields_and_secret():
    assert EmailDestination.secret_keys() == {"password"}
    keys = {f["key"] for f in EmailDestination.FIELDS}
    assert {"host", "port", "security", "username", "password", "from", "to",
            "subject", "max_attachment_mb"} <= keys
    assert "password" not in EmailDestination.public_options({"host": "h", "password": "x"})


def test_deliver_sends_attachment(sink):
    loc, confirmed = EmailDestination().deliver(PDF, "scan_001.pdf", opts(sink))
    assert confirmed and "a@example.com" in loc
    authed, raw = sink.messages[0]
    assert authed
    text = raw.decode()
    assert "Subject: Scan scan_001.pdf" in text
    assert "To: a@example.com, b@example.com" in text
    assert 'filename="scan_001.pdf"' in text
    assert base64.b64encode(PDF).decode()[:20] in text.replace("\r\n", "")


def test_too_large_fails_plainly(sink):
    big = b"x" * (1024 * 1024 + 1)
    with pytest.raises(DeliveryError, match="above the 1 MB email limit"):
        EmailDestination().deliver(big, "big.pdf", opts(sink))
    assert sink.messages == []


def test_bad_login_message_and_no_secret_in_logs(sink, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(DeliveryError) as e:
        EmailDestination().deliver(PDF, "a.pdf", opts(sink, password="wrong-pw-123"))
    assert "rejected the user name or password" in str(e.value)
    assert "wrong-pw-123" not in str(e.value) + caplog.text


def test_test_logs_in_without_sending(sink):
    r = EmailDestination().test(opts(sink))
    assert r["ok"] and sink.logins == 1 and sink.messages == []
    bad = EmailDestination().test(opts(sink, password="nope"))
    assert not bad["ok"] and "user name or password" in bad["message"]


def test_unreachable_and_missing_fields(sink):
    port = sink.port
    sink.shutdown()
    sink.server_close()
    with pytest.raises(DeliveryError, match="Could not reach"):
        EmailDestination().deliver(PDF, "a.pdf", {"host": "127.0.0.1", "port": str(port),
                                                  "security": "none", "from": "a@b.c", "to": ["x@y.z"]})
    with pytest.raises(DeliveryError, match="No mail server"):
        EmailDestination().deliver(PDF, "a.pdf", {})
    with pytest.raises(DeliveryError, match="not a valid email"):
        EmailDestination().deliver(PDF, "a.pdf", {"host": "h", "from": "a@b.c", "to": ["nope"]})


def test_starttls_refused_gives_plain_message(sink):
    # The sink offers no STARTTLS, so the secure setting must fail with a plain message.
    with pytest.raises(DeliveryError, match="security"):
        EmailDestination().deliver(PDF, "a.pdf", opts(sink, security="starttls"))
