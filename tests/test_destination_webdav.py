"""WebDAV destination against a small stdlib WebDAV server started inside the test."""

import base64
import datetime
import http.server
import logging
import shutil
import ssl
import subprocess
import threading
from pathlib import Path

import pytest

from openscan.destinations import DeliveryError
from openscan.destinations.webdav import WebdavDestination

PDF = b"%PDF-1.4 dav\n%%EOF"
AUTH = "Basic " + base64.b64encode(b"alice:dav-pass-xyz").decode()


def make_handler(root: Path):
    class H(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _path(self):
            from urllib.parse import unquote
            return root / unquote(self.path).lstrip("/")

        def _reply(self, code, body=b""):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _check(self):
            n = int(self.headers.get("Content-Length") or 0)
            self.body = self.rfile.read(n) if n else b""
            if self.headers.get("Authorization") != AUTH:
                self._reply(401)
                return False
            return True

        def do_PUT(self):
            if not self._check():
                return
            p = self._path()
            if not p.parent.is_dir():
                return self._reply(409)
            if p.exists() and self.headers.get("If-None-Match") == "*":
                return self._reply(412)
            p.write_bytes(self.body)
            self._reply(201)

        def do_MKCOL(self):
            if not self._check():
                return
            p = self._path()
            if p.exists():
                return self._reply(405)
            if not p.parent.is_dir():
                return self._reply(409)
            p.mkdir()
            self._reply(201)

        def do_PROPFIND(self):
            if not self._check():
                return
            if not self._path().exists():
                return self._reply(404)
            self._reply(207, b'<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>')

    return H


@pytest.fixture
def dav(tmp_path):
    root = tmp_path / "dav"
    root.mkdir()
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield srv, root
    srv.shutdown()
    srv.server_close()


def opts(srv, **kw):
    base = {"url": f"http://127.0.0.1:{srv.server_address[1]}/remote", "username": "alice",
            "password": "dav-pass-xyz", "path": "Scans/2026"}
    base.update(kw)
    return base


def test_fields():
    assert WebdavDestination.secret_keys() == {"password"}
    assert {"url", "path", "username", "password", "allow_self_signed"} <= {
        f["key"] for f in WebdavDestination.FIELDS}


def test_deliver_creates_folders_and_avoids_collisions(dav):
    srv, root = dav
    (root / "remote").mkdir()
    d = WebdavDestination()
    loc, ok = d.deliver(PDF, "scan_001.pdf", opts(srv))
    assert ok and loc.endswith("/Scans/2026/scan_001.pdf")
    loc2, _ = d.deliver(b"%PDF second", "scan_001.pdf", opts(srv))
    assert loc2.endswith("scan_002.pdf")
    folder = root / "remote" / "Scans" / "2026"
    assert (folder / "scan_001.pdf").read_bytes() == PDF
    assert (folder / "scan_002.pdf").read_bytes() == b"%PDF second"


def test_plain_name_collision(dav):
    srv, root = dav
    (root / "remote").mkdir()
    d = WebdavDestination()
    d.deliver(PDF, "doc.pdf", opts(srv, path=""))
    loc, _ = d.deliver(PDF, "doc.pdf", opts(srv, path=""))
    assert loc.endswith("doc_2.pdf")


def test_wrong_password_and_log(dav, caplog):
    srv, root = dav
    caplog.set_level(logging.DEBUG)
    with pytest.raises(DeliveryError, match="rejected the user name or password"):
        WebdavDestination().deliver(PDF, "a.pdf", opts(srv, password="bad-pass-777"))
    assert "bad-pass-777" not in caplog.text


def test_test_does_propfind(dav):
    srv, root = dav
    (root / "remote").mkdir()
    assert WebdavDestination().test(opts(srv))["ok"]
    assert "first scan" in WebdavDestination().test(opts(srv))["message"]
    r = WebdavDestination().test(opts(srv, password="x"))
    assert not r["ok"] and "user name or password" in r["message"]


def test_unreachable_and_missing_url():
    with pytest.raises(DeliveryError, match="Could not reach"):
        WebdavDestination().deliver(PDF, "a.pdf", {"url": "http://127.0.0.1:1/x"})
    with pytest.raises(DeliveryError, match="No WebDAV address"):
        WebdavDestination().deliver(PDF, "a.pdf", {})
    with pytest.raises(DeliveryError, match="'..'"):
        WebdavDestination().deliver(PDF, "a.pdf", {"url": "http://h/x", "path": "../up"})


@pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl is not installed")
def test_self_signed_certificate(tmp_path):
    root = tmp_path / "dav"
    (root / "remote").mkdir(parents=True)
    key, crt = tmp_path / "k.pem", tmp_path / "c.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key),
                    "-out", str(crt), "-days", "2", "-subj", "/CN=127.0.0.1",
                    "-addext", "subjectAltName=IP:127.0.0.1"], check=True, capture_output=True)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), make_handler(root))
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(crt), str(key))
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        o = {"url": f"https://127.0.0.1:{srv.server_address[1]}/remote", "username": "alice",
             "password": "dav-pass-xyz"}
        with pytest.raises(DeliveryError, match="not trusted"):
            WebdavDestination().deliver(PDF, "s.pdf", o)
        assert not WebdavDestination().test(o)["ok"]
        WebdavDestination().deliver(PDF, "s.pdf", {**o, "allow_self_signed": True})
        assert (root / "remote" / "s.pdf").read_bytes() == PDF
    finally:
        srv.shutdown()
        srv.server_close()


def test_bad_port_in_address_fails_plainly():
    # http.client raises InvalidURL (a ValueError) for a port that is not a number.
    with pytest.raises(DeliveryError, match="not valid"):
        WebdavDestination().deliver(PDF, "a.pdf", {"url": "https://nas.example:dav/remote.php"})
    r = WebdavDestination().test({"url": "https://nas.example:dav/remote.php"})
    assert not r["ok"] and "not valid" in r["message"]
