"""SMB destination.

Real tests run against a Samba server started inside the test (needs smbd; no root needed). They skip when smbd is missing. The mocked
tests always run and cover option handling and error text.
"""

import getpass
import logging
import os
import shutil
import subprocess
import time

import pytest

from openscan.destinations import DeliveryError
from openscan.destinations import smb as smbmod
from openscan.destinations.smb import SmbDestination

PDF = b"%PDF-1.4 smb\n%%EOF"
PASSWORD = "smb-pass-Q7x"
PORT = 14455
USER = getpass.getuser()

needs_samba = pytest.mark.skipif(
    shutil.which("smbd") is None and not os.path.exists("/usr/sbin/smbd"),
    reason="needs smbd")


@pytest.fixture
def samba(tmp_path):
    exe = shutil.which("smbd") or "/usr/sbin/smbd"
    pdbedit = shutil.which("pdbedit") or "/usr/bin/pdbedit"
    share = tmp_path / "share"
    share.mkdir()
    state = tmp_path / "state"
    for d in ("lock", "state", "cache", "private", "pid", "log", "ncalrpc"):
        (state / d).mkdir(parents=True)
    conf = tmp_path / "smb.conf"
    conf.write_text(f"""[global]
  workgroup = TESTGROUP
  server role = standalone server
  security = user
  map to guest = never
  smb ports = {PORT}
  bind interfaces only = yes
  interfaces = lo
  disable netbios = yes
  load printers = no
  printing = bsd
  printcap name = /dev/null
  lock directory = {state}/lock
  state directory = {state}/state
  cache directory = {state}/cache
  private dir = {state}/private
  pid directory = {state}/pid
  ncalrpc dir = {state}/ncalrpc
  log file = {state}/log/%m.log
  passdb backend = tdbsam:{state}/private/passdb.tdb
[scans]
  path = {share}
  read only = no
  valid users = {USER}
""")
    subprocess.run([pdbedit, "-s", str(conf), "-a", "-t", "-u", USER], input=f"{PASSWORD}\n{PASSWORD}\n",
                   text=True, check=True, capture_output=True)
    proc = subprocess.Popen([exe, "-F", "-s", str(conf), "--no-process-group"],
                            start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    import socket
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.2)
    else:
        os.killpg(proc.pid, 9)
        pytest.skip("smbd did not start")
    yield share
    import smbclient
    smbclient.reset_connection_cache()
    os.killpg(proc.pid, 15)
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, 9)


def opts(**kw):
    base = {"server": f"127.0.0.1:{PORT}", "share": "scans", "path": "Inbox/2026",
            "username": USER, "password": PASSWORD}
    base.update(kw)
    return base


def test_fields():
    assert SmbDestination.secret_keys() == {"password"}
    assert [f["key"] for f in SmbDestination.FIELDS] == [
        "server", "share", "path", "username", "password", "domain"]


@needs_samba
def test_real_deliver_and_collision(samba):
    d = SmbDestination()
    loc, ok = d.deliver(PDF, "scan_001.pdf", opts())
    assert ok and loc.endswith("Inbox\\2026\\scan_001.pdf")
    loc2, _ = d.deliver(b"%PDF other", "scan_001.pdf", opts())
    assert loc2.endswith("scan_002.pdf")
    d.deliver(PDF, "doc.pdf", opts())
    loc3, _ = d.deliver(PDF, "doc.pdf", opts())
    assert loc3.endswith("doc_2.pdf")
    folder = samba / "Inbox" / "2026"
    assert (folder / "scan_001.pdf").read_bytes() == PDF
    assert (folder / "scan_002.pdf").read_bytes() == b"%PDF other"


@needs_samba
def test_real_test_writes_and_deletes_probe(samba):
    r = SmbDestination().test(opts())
    assert r["ok"], r
    assert not [p for p in (samba / "Inbox" / "2026").iterdir()]


@needs_samba
def test_real_errors_are_plain_and_hide_password(samba, caplog):
    caplog.set_level(logging.DEBUG)
    with pytest.raises(DeliveryError, match="rejected the user name or password"):
        SmbDestination().deliver(PDF, "a.pdf", opts(password="wrong-Pw-999"))
    assert "wrong-Pw-999" not in caplog.text
    with pytest.raises(DeliveryError, match="was not found"):
        SmbDestination().deliver(PDF, "a.pdf", opts(share="nosuchshare"))
    r = SmbDestination().test(opts(password="wrong-Pw-999"))
    assert not r["ok"] and "user name or password" in r["message"]


def test_unreachable_server_message():
    with pytest.raises(DeliveryError, match="Could not reach the SMB server"):
        SmbDestination().deliver(PDF, "a.pdf", {"server": "127.0.0.1:1", "share": "s"})


def test_option_checks():
    with pytest.raises(DeliveryError, match="No SMB server"):
        SmbDestination().deliver(PDF, "a.pdf", {"share": "s"})
    with pytest.raises(DeliveryError, match="No share"):
        SmbDestination().deliver(PDF, "a.pdf", {"server": "h"})
    with pytest.raises(DeliveryError, match="'..'"):
        SmbDestination().deliver(PDF, "a.pdf", {"server": "h", "share": "s", "path": "../x"})
    assert not SmbDestination().test({})["ok"]


def test_domain_joins_user_and_mock_session(monkeypatch):
    calls = {}

    class FakeFile:
        def __init__(self, path):
            self.path = path
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def write(self, data): calls["data"] = data

    class FakeClient:
        taken = {"\\\\h\\s\\a.pdf"}
        def stat(self, path, **kw): return None
        def mkdir(self, path, **kw): pass
        def open_file(self, path, mode, **kw):
            if path in self.taken:
                raise OSError(17, "exists", path)
            calls["path"] = path
            return FakeFile(path)

    def fake_session(s):
        calls.update(user=s["username"], pw=s["password"])
        return FakeClient()

    monkeypatch.setattr(smbmod, "_session", fake_session)
    loc, _ = SmbDestination().deliver(PDF, "a.pdf", {
        "server": "h", "share": "s", "username": "bob", "password": "pw", "domain": "CORP"})
    assert calls["user"] == "CORP\\bob" and loc == "\\\\h\\s\\a_2.pdf"
    assert calls["data"] == PDF
