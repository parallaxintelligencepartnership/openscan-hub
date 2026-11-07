"""SMB share poll. Most tests talk to a real Samba smbd that the fixture
starts without root on a free port; they are skipped where smbd is missing.
The unit tests at the end need no server."""

import getpass
import json
import os
import shutil
import socket
import subprocess
import time
from pathlib import Path

import pytest

from openscan import retry_queue
from openscan.config import AppConfig
from openscan.scanner import smb_poll

smbclient = pytest.importorskip("smbclient")

PDF = b"%PDF-1.4 smb scan\n%%EOF"
PASSWORD = "smb-Pass-12345"
SMBD = shutil.which("smbd") or ("/usr/sbin/smbd" if os.path.exists("/usr/sbin/smbd") else None)
PDBEDIT = shutil.which("pdbedit") or ("/usr/bin/pdbedit" if os.path.exists("/usr/bin/pdbedit") else None)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def samba(tmp_path_factory):
    if not (SMBD and PDBEDIT):
        pytest.skip("smbd is not installed")
    base = tmp_path_factory.mktemp("samba")
    share = base / "share"
    share.mkdir()
    for d in ("priv", "lock", "state", "cache", "pid", "ncalrpc"):
        (base / d).mkdir(mode=0o700)
    port = free_port()
    conf = base / "smb.conf"
    conf.write_text(f"""[global]
    workgroup = TEST
    server role = standalone server
    security = user
    map to guest = never
    smb ports = {port}
    interfaces = 127.0.0.1
    bind interfaces only = yes
    disable netbios = yes
    load printers = no
    printing = bsd
    printcap name = /dev/null
    disable spoolss = yes
    log file = {base}/log.%m
    private dir = {base}/priv
    lock directory = {base}/lock
    state directory = {base}/state
    cache directory = {base}/cache
    pid directory = {base}/pid
    ncalrpc dir = {base}/ncalrpc
    passdb backend = tdbsam:{base}/priv/passdb.tdb
    server min protocol = SMB2
[scans]
    path = {share}
    read only = no
    valid users = {getpass.getuser()}
""")
    user = getpass.getuser()
    subprocess.run([PDBEDIT, "-s", str(conf), "-a", "-u", user, "-t"], input=f"{PASSWORD}\n{PASSWORD}\n",
                   text=True, check=True, capture_output=True)
    proc = subprocess.Popen([SMBD, "-F", "--no-process-group", "-s", str(conf), "-l", str(base)],
                            stdout=(base / "smbd.out").open("wb"), stderr=subprocess.STDOUT,
                            start_new_session=True)  # smbd signals its whole process group
    end = time.monotonic() + 15
    while time.monotonic() < end:
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        proc.kill()
        pytest.skip("smbd did not start: " + (base / "smbd.out").read_text(errors="replace")[-300:]
                    + "".join(f.read_text(errors="replace")[-600:] for f in base.glob("log.*")))
    yield {"port": port, "share": share, "user": user}
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture
def queue(tmp_path, monkeypatch):
    q = retry_queue.RetryQueue(tmp_path / "q" / "retry_queue.json", tmp_path / "q" / "files")
    monkeypatch.setattr(retry_queue, "_queue", q)
    yield q
    q.stop()


@pytest.fixture
def box(samba, config_dir, tmp_path, queue):
    """A clean share folder and a config that points at it."""
    folder = samba["share"] / "inbox"
    shutil.rmtree(folder, ignore_errors=True)
    folder.mkdir()
    cfg = AppConfig()
    cfg.wizard_completed = True
    cfg.output.folder = str(tmp_path / "out")
    s = cfg.smb_poll
    s.enabled = True
    s.server = "127.0.0.1"
    s.share = "scans"
    s.path = "inbox"
    s.username = samba["user"]
    s.password = PASSWORD
    s.port = samba["port"]
    s.poll_interval = 0.1
    smb_poll._stop.clear()
    yield cfg, folder
    smb_poll.stop_smb_poll()
    smbclient.reset_connection_cache()


def polls(cfg, n, seen, state):
    for _ in range(n):
        smb_poll.poll_once(cfg, seen, state)


def test_file_waits_for_a_stable_size_then_is_delivered_once(box, tmp_path):
    cfg, folder = box
    seen, state = {}, {}
    (folder / "a.pdf").write_bytes(PDF)
    smb_poll.poll_once(cfg, seen, state)
    assert not (tmp_path / "out" / "a.pdf").exists()  # first sighting only
    (folder / "a.pdf").write_bytes(PDF + b"x")  # still growing
    smb_poll.poll_once(cfg, seen, state)
    assert not (tmp_path / "out" / "a.pdf").exists()
    smb_poll.poll_once(cfg, seen, state)  # same size twice
    assert (tmp_path / "out" / "a.pdf").read_bytes() == PDF + b"x"
    assert not (folder / "a.pdf").exists()
    assert (folder / "processed" / "a.pdf").read_bytes() == PDF + b"x"


def test_delete_action(box, tmp_path):
    cfg, folder = box
    cfg.smb_poll.after_delivery = "delete"
    (folder / "b.pdf").write_bytes(PDF)
    polls(cfg, 2, {}, {})
    assert (tmp_path / "out" / "b.pdf").exists()
    assert list(folder.iterdir()) == []


def test_ignores_other_files_and_subfolders(box, tmp_path):
    cfg, folder = box
    (folder / "notes.txt").write_bytes(b"hi")
    (folder / "sub.pdf").mkdir()
    polls(cfg, 3, {}, {})
    assert not (tmp_path / "out").exists() or list((tmp_path / "out").iterdir()) == []
    assert (folder / "notes.txt").exists()


def test_never_delivers_twice_after_restart_when_move_fails(box, tmp_path, monkeypatch):
    cfg, folder = box
    (folder / "c.pdf").write_bytes(PDF)
    seen, state = {}, {}
    with monkeypatch.context() as m:  # only the move fails; the share is real
        m.setattr(smbclient, "rename", lambda *a, **k: (_ for _ in ()).throw(OSError("denied")))
        polls(cfg, 3, seen, state)
    assert (folder / "c.pdf").exists()  # could not move
    delivered = list((tmp_path / "out").iterdir())
    assert len(delivered) == 1
    # Restart: new memory, state read back from the config folder.
    state2 = smb_poll.load_state()
    assert any("c.pdf" in k for k in state2)
    polls(cfg, 3, {}, state2)
    assert len(list((tmp_path / "out").iterdir())) == 1  # no second copy
    assert not (folder / "c.pdf").exists()  # now cleared off the share


def test_changed_file_with_same_name_is_delivered_again(box, tmp_path):
    cfg, folder = box
    cfg.smb_poll.after_delivery = "delete"
    (folder / "d.pdf").write_bytes(PDF)
    seen, state = {}, {}
    polls(cfg, 2, seen, state)
    (folder / "d.pdf").write_bytes(PDF + b"more bytes")
    polls(cfg, 2, seen, state)
    assert len(list((tmp_path / "out").iterdir())) == 2


def test_failed_delivery_is_retried_not_marked_handled(box, tmp_path, queue, monkeypatch):
    cfg, folder = box
    from openscan import intake
    real = intake.handle_scan

    class Fail:
        ok = False
        queued = False
        errors = ["disk full"]
        filename = ""
        size_bytes = 0

        def to_dict(self):
            return {"ok": False, "saved_paths": [], "errors": self.errors, "queued": False}

    monkeypatch.setattr(intake, "handle_scan", lambda *a, **k: Fail())
    (folder / "e.pdf").write_bytes(PDF)
    seen, state = {}, {}
    polls(cfg, 3, seen, state)
    assert (folder / "e.pdf").exists() and not state
    monkeypatch.setattr(intake, "handle_scan", real)
    polls(cfg, 2, seen, state)
    assert (tmp_path / "out" / "e.pdf").exists()


def test_wrong_password_is_reported_and_nothing_breaks(box):
    cfg, folder = box
    cfg.smb_poll.password = "wrong-password"
    (folder / "f.pdf").write_bytes(PDF)
    polls(cfg, 2, {}, {})
    err = smb_poll.smb_poll_last_error()
    assert err and "wrong-password" not in err
    assert (folder / "f.pdf").exists()


def test_service_loop_delivers_and_stops(box, tmp_path):
    cfg, folder = box
    assert smb_poll.start_smb_poll(cfg)
    assert smb_poll.smb_poll_running()
    (folder / "g.pdf").write_bytes(PDF)
    end = time.monotonic() + 15
    while time.monotonic() < end and not (tmp_path / "out" / "g.pdf").exists():
        time.sleep(0.1)
    smb_poll.stop_smb_poll()
    assert (tmp_path / "out" / "g.pdf").exists()
    assert not smb_poll.smb_poll_running()


# --- no server needed --------------------------------------------------------

def test_start_refuses_missing_settings(config_dir):
    cfg = AppConfig()
    cfg.smb_poll.enabled = True
    assert smb_poll.start_smb_poll(cfg) is False
    assert "server" in smb_poll.smb_poll_error()
    cfg.smb_poll.server = "nas"
    assert smb_poll.start_smb_poll(cfg) is False
    assert "share" in smb_poll.smb_poll_error()
    assert not smb_poll.smb_poll_running()


def test_unc_paths():
    cfg = AppConfig()
    cfg.smb_poll.server = "nas"
    cfg.smb_poll.share = "scans"
    cfg.smb_poll.path = "/in//box/"
    assert smb_poll.folder_unc(cfg) == "\\\\nas\\scans\\in\\box"
    cfg.smb_poll.path = ""
    assert smb_poll.folder_unc(cfg) == "\\\\nas\\scans"


def test_state_file_round_trip(config_dir):
    smb_poll.save_state({"k": [1, 2]})
    assert json.loads((config_dir / smb_poll.STATE_FILE).read_text()) == {"k": [1, 2]}
    assert smb_poll.load_state() == {"k": [1, 2]}


def test_raising_second_destination_gives_one_copy_and_a_queue_entry(box, tmp_path, queue):
    # Review probe p5: [folder, WebDAV with a port that is not a number] made a
    # new folder copy on every poll.
    from openscan.profiles import Profile
    cfg, folder = box
    out = tmp_path / "dup"
    cfg.profiles = [Profile(id="p", name="P", follow_settings=False, destinations=[
        {"type": "folder", "options": {"folder": str(out)}},
        {"type": "webdav", "options": {"url": "https://nas.example:dav/remote.php"}}])]
    cfg.default_profile_id = "p"
    (folder / "c.pdf").write_bytes(PDF)
    polls(cfg, 6, {}, {})
    assert sorted(p.name for p in out.iterdir()) == ["c.pdf"]
    assert not (folder / "c.pdf").exists()
    assert [e["destination"] for e in queue.pending()] == ["webdav"]


def test_document_neither_delivered_nor_queued_is_not_marked_handled(box, monkeypatch):
    from openscan import intake
    from test_intake import partial_result
    cfg, folder = box
    monkeypatch.setattr(intake, "handle_scan", lambda *a, **k: partial_result())
    (folder / "h.pdf").write_bytes(PDF)
    state = {}
    polls(cfg, 3, {}, state)
    assert not state and (folder / "h.pdf").exists()


def test_smb_destination_does_not_close_the_poll_session(box, samba, tmp_path):
    # Review probe p5 (A1): the SMB destination reset the global session
    # cache, so each poll delivered one file and the move after it failed.
    from openscan.profiles import Profile
    cfg, folder = box
    cfg.profiles = [Profile(id="p", name="P", follow_settings=False, destinations=[
        {"type": "smb", "options": {"server": f"127.0.0.1:{samba['port']}", "share": "scans",
                                    "path": "out-a1", "username": samba["user"],
                                    "password": PASSWORD}}])]
    cfg.default_profile_id = "p"
    out = samba["share"] / "out-a1"
    shutil.rmtree(out, ignore_errors=True)
    (folder / "a.pdf").write_bytes(PDF)
    (folder / "b.pdf").write_bytes(PDF + b"b")
    seen, state = {}, {}
    polls(cfg, 2, seen, state)  # first sighting, then deliver both
    assert sorted(p.name for p in out.iterdir()) == ["a.pdf", "b.pdf"]
    assert sorted(p.name for p in folder.iterdir()) == ["processed"]


def test_file_above_the_size_limit_stays_on_the_share(box, tmp_path, monkeypatch):
    # Review A10: a huge file must not be read into memory.
    cfg, folder = box
    monkeypatch.setattr(smb_poll, "MAX_FILE_BYTES", 10)
    opened = []
    real_open = smbclient.open_file
    monkeypatch.setattr(smbclient, "open_file", lambda *a, **k: opened.append(a[0]) or real_open(*a, **k))
    (folder / "big.pdf").write_bytes(PDF)
    polls(cfg, 3, {}, {})
    assert opened == []
    assert (folder / "big.pdf").exists() and not (tmp_path / "out").exists()
