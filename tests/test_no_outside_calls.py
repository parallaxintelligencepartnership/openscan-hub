"""OpenScanHub makes no outside calls. Only scanner, destination and input
modules may use a network client. A new module that does needs a line here."""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "openscan"

CLIENT_PATTERNS = ("urlopen", "http.client", "smtplib", "imaplib", "socket.create_connection",
                   "smbclient", "smbprotocol")

# Modules that talk to a scanner, a destination or an input source the user set up.
ALLOWED = {
    # scanners
    "discovery.py",
    "scanner/escl.py",
    "scanner/wsd.py",
    "scanner/ftp_receive.py",
    "scanner/folder_watch.py",
    "scanner/smb_poll.py",
    "scanner/imap_receive.py",
    # destinations
    "destinations/paperless.py",
    "destinations/smb.py",
    "destinations/webdav.py",
    "destinations/email.py",
    # the optional Home Assistant module
    "integrations/homeassistant.py",
}

# "http.client" also matches the docs in comments; look at code lines only.
_RX = re.compile("|".join(re.escape(p) for p in CLIENT_PATTERNS))


def _uses_client(path: Path) -> list[str]:
    hits = []
    for line in path.read_text(encoding="utf-8").splitlines():
        code = line.split("#", 1)[0]
        if _RX.search(code):
            hits.append(line.strip())
    return hits


def test_only_listed_modules_use_a_network_client():
    offenders = {}
    for path in sorted(PKG.rglob("*.py")):
        rel = path.relative_to(PKG).as_posix()
        hits = _uses_client(path)
        if hits and rel not in ALLOWED:
            offenders[rel] = hits[:3]
    assert not offenders, f"network client use outside the allow list: {offenders}"


def test_allow_list_has_no_stale_core_modules():
    # Core modules must stay off the list.
    for core in ("intake.py", "audit.py", "edition.py", "main.py", "config.py", "profiles.py",
                 "naming.py", "web/server.py", "web/audit_api.py"):
        assert core not in ALLOWED


def test_no_update_check_or_usage_report():
    for path in PKG.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "check_for_update" not in text and "telemetry" not in text, path
