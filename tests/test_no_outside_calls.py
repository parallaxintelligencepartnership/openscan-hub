"""OpenScanHub makes no outside calls. Only scanner, destination and input
modules may use a network client. A new module that does needs a line here."""

import re
from pathlib import Path

PKG = Path(__file__).resolve().parent.parent / "openscan"

# Regular expressions for code that can open a network connection.
CLIENT_PATTERNS = (
    r"urlopen", r"urllib\.request", r"build_opener", r"http\.client", r"smtplib", r"imaplib",
    r"ftplib", r"poplib", r"telnetlib", r"create_connection", r"socket\.socket",
    r"open_connection", r"smbclient", r"smbprotocol", r"\bhttpx\b", r"\baiohttp\b",
    r"\burllib3\b", r"\b(?:import|from)\s+requests\b", r"\brequests\.\w+\(",
)

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

# Core modules that may use one client for a local purpose only. A hit line
# in such a module must contain the text given here.
LOCAL_ONLY = {
    # UDP connect to a documentation address picks the LAN interface; it sends nothing.
    "web/server.py": ("socket.socket(socket.AF_INET, socket.SOCK_DGRAM)",),
    "mdns.py": ("socket.socket(socket.AF_INET, socket.SOCK_DGRAM)",),
    # Asks the already running copy on 127.0.0.1 to open its window.
    "singleinstance.py": ("urllib.request",),
}

# "http.client" also matches the docs in comments; look at code lines only.
_RX = re.compile("|".join(CLIENT_PATTERNS))


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
        if rel in LOCAL_ONLY:
            hits = [h for h in hits if not any(ok in h for ok in LOCAL_ONLY[rel])]
        if hits and rel not in ALLOWED:
            offenders[rel] = hits[:3]
    assert not offenders, f"network client use outside the allow list: {offenders}"


def test_patterns_see_every_common_client(tmp_path):
    # Review A6: the old list missed build_opener, raw sockets, ftplib, requests and httpx.
    lines = [
        "opener = urllib.request.build_opener()",
        "s = socket.socket(); s.connect(('example.com', 80))",
        "import ftplib",
        "import requests",
        "r = requests.get(url)",
        "import httpx",
        "import aiohttp",
        "reader, writer = await asyncio.open_connection(host, 80)",
        "socket.create_connection((host, 80))",
    ]
    f = tmp_path / "m.py"
    f.write_text("\n".join(lines + ["# import requests in a comment", "x = 'web requests'"]))
    assert _uses_client(f) == lines


def test_allow_list_has_no_stale_core_modules():
    # Core modules must stay off the list.
    for core in ("intake.py", "audit.py", "edition.py", "main.py", "config.py", "profiles.py",
                 "naming.py", "web/server.py", "web/audit_api.py"):
        assert core not in ALLOWED


def test_no_update_check_or_usage_report():
    for path in PKG.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        assert "check_for_update" not in text and "telemetry" not in text, path
