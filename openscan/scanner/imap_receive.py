"""Email in - take scan attachments from a mailbox.

Some scanners and phone apps send "Scan to email". OpenScanHub polls an
IMAP mailbox, takes the PDF and image attachments of unseen messages,
routes each through the standard pipeline, marks the message seen and
moves it to another folder.

Uses the standard library imaplib. The connection checks the server
certificate. Passwords are never logged.
"""

import email
import email.policy
import imaplib
import logging
import ssl
import threading
from datetime import datetime
from email.utils import parseaddr
from pathlib import Path
from typing import Optional

from ..config import AppConfig
from .. import intake
from .ftp_receive import sanitize_filename

logger = logging.getLogger(__name__)

SCAN_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}
SCAN_TYPES = {"application/pdf", "image/jpeg", "image/png", "image/tiff"}
CONNECT_TIMEOUT = 30

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_lock = threading.Lock()
_error: Optional[str] = None
_last_error: Optional[str] = None


def imap_receive_running() -> bool:
    return bool(_thread and _thread.is_alive())


def imap_receive_error() -> Optional[str]:
    """Why the receiver refused to start (None if fine)."""
    return _error


def imap_receive_last_error() -> Optional[str]:
    """The problem of the last poll (None if the last poll worked)."""
    return _last_error


def check_settings(config: AppConfig) -> Optional[str]:
    c = config.imap_receive
    if not c.host.strip():
        return "Enter the mail server name."
    if not c.username.strip():
        return "Enter the mail user name."
    if c.security not in ("ssl", "starttls"):
        return "Choose SSL or STARTTLS."
    if not c.folder.strip():
        return "Enter the mailbox folder to read."
    return None


def _ssl_context() -> ssl.SSLContext:
    """Checks the certificate and the host name. Tests replace this."""
    return ssl.create_default_context()


def sender_allowed(from_header: str, allowed: list[str]) -> bool:
    """True if `allowed` is empty or names the sender. An entry is a full
    address, or a domain written as "@example.com" or "example.com"."""
    entries = [a.strip().lower() for a in allowed if a.strip()]
    if not entries:
        return True
    addr = parseaddr(from_header)[1].strip().lower()
    if not addr:
        return False
    domain = addr.rpartition("@")[2]
    for e in entries:
        if "@" in e and not e.startswith("@"):
            if addr == e:
                return True
        elif domain == e.lstrip("@"):
            return True
    return False


def extract_attachments(msg) -> list[tuple[str, bytes]]:
    """(filename, bytes) of each PDF or image attachment, in message order."""
    out = []
    for i, part in enumerate(msg.walk(), 1):
        if part.is_multipart():
            continue
        ctype = part.get_content_type()
        name = part.get_filename() or ""
        ext = Path(name).suffix.lower()
        disposition = part.get_content_disposition()
        if not (ctype in SCAN_TYPES or (ext in SCAN_EXTENSIONS
                                        and ctype in ("application/octet-stream", "binary/octet-stream"))):
            continue
        if not name and disposition != "attachment":
            continue  # an inline picture such as a logo has no file name
        data = part.get_payload(decode=True)
        if not data:
            continue
        if not name:
            name = f"attachment_{i}" + {"application/pdf": ".pdf", "image/jpeg": ".jpg",
                                         "image/png": ".png", "image/tiff": ".tif"}.get(ctype, "")
        out.append((sanitize_filename(name, default="attachment"), data))
    return out


def _quote(folder: str) -> str:
    return '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _connect(config: AppConfig) -> imaplib.IMAP4:
    c = config.imap_receive
    ctx = _ssl_context()
    if c.security == "ssl":
        conn = imaplib.IMAP4_SSL(c.host.strip(), c.port, ssl_context=ctx, timeout=CONNECT_TIMEOUT)
    else:
        conn = imaplib.IMAP4(c.host.strip(), c.port, timeout=CONNECT_TIMEOUT)
        try:
            conn.starttls(ssl_context=ctx)
        except BaseException:
            _quiet_close(conn)
            raise
    try:
        conn.login(c.username, c.password)
    except BaseException:
        _quiet_close(conn)
        raise
    return conn


def _quiet_close(conn) -> None:
    try:
        conn.shutdown()
    except Exception:
        pass


def _plain(e: BaseException, config: AppConfig) -> str:
    text = (str(e) or e.__class__.__name__).splitlines()[0][:200]
    password = config.imap_receive.password
    if password:
        text = text.replace(password, "***")
    return text


def poll_once(config: AppConfig, skipped: Optional[set] = None) -> int:
    """One poll. Returns how many messages were delivered and cleared."""
    global _last_error
    skipped = skipped if skipped is not None else set()
    c = config.imap_receive
    try:
        conn = _connect(config)
    except Exception as e:
        _last_error = _plain(e, config)
        logger.error("Mail poll could not connect to %s: %s", c.host, _last_error)
        return 0
    done = 0
    try:
        typ, _ = conn.select(_quote(c.folder))
        if typ != "OK":
            raise imaplib.IMAP4.error(f"cannot open folder {c.folder}")
        typ, data = conn.uid("SEARCH", None, "UNSEEN")
        if typ != "OK":
            raise imaplib.IMAP4.error("search failed")
        uids = data[0].split() if data and data[0] else []
        if c.move_to_folder.strip():
            _ensure_folder(conn, c.move_to_folder.strip())
        for uid in uids:
            if _stop.is_set():
                break
            if uid in skipped:
                continue
            outcome = _handle_message(config, conn, uid)
            if outcome == "done":
                done += 1
            elif outcome == "skip":
                skipped.add(uid)  # not for us; do not fetch it again this run
        _last_error = None
    except Exception as e:
        _last_error = _plain(e, config)
        logger.error("Mail poll failed: %s", _last_error)
    finally:
        try:
            conn.logout()
        except Exception:
            _quiet_close(conn)
    return done


def _ensure_folder(conn, folder: str) -> None:
    typ, data = conn.list("", _quote(folder))
    if typ == "OK" and data and data[0]:
        return
    conn.create(_quote(folder))


def _handle_message(config: AppConfig, conn, uid: bytes) -> str:
    """Deliver the attachments of one message. Returns "done" (seen and
    moved), "skip" (not allowed or nothing to take: left unseen) or "retry"
    (a delivery failed: left unseen, tried again next poll)."""
    c = config.imap_receive
    typ, data = conn.uid("FETCH", uid, "(BODY.PEEK[])")
    if typ != "OK" or not data or not isinstance(data[0], tuple):
        return "retry"
    msg = email.message_from_bytes(data[0][1], policy=email.policy.default)
    sender = str(msg.get("From", ""))
    if not sender_allowed(sender, c.allowed_senders):
        logger.info("Mail from a sender that is not allowed was left alone")
        return "skip"
    attachments = extract_attachments(msg)
    if not attachments:
        return "skip"
    all_ok = True
    for filename, payload in attachments:
        all_ok &= _deliver(config, filename, payload)
    if not all_ok:
        return "retry"
    conn.uid("STORE", uid, "+FLAGS", "(\\Seen)")
    target = c.move_to_folder.strip()
    if target and target != c.folder:
        typ, _ = conn.uid("COPY", uid, _quote(target))
        if typ == "OK":
            conn.uid("STORE", uid, "+FLAGS", "(\\Deleted)")
            conn.expunge()
        else:
            logger.error("Could not move a message to %s", target)
    return "done"


def _deliver(config: AppConfig, filename: str, data: bytes) -> bool:
    try:
        result = intake.handle_scan(config, data, source="imap_receive", started_by="email",
                                    filename=filename)
    except Exception:
        logger.exception("Failed to route mail attachment %s", filename)
        _add_history(filename, len(data), None, "internal error")
        return False
    _add_history(result.filename or filename, result.size_bytes or len(data), result, None)
    if result.ok:
        logger.info("Mail scan routed: %s", filename)
        return True
    if intake.handled(result):
        logger.warning("Mail scan %s is in the retry queue", filename)
        return True
    logger.error("Could not deliver %s: %s", filename, "; ".join(result.errors))
    return False


def start_imap_receive(config: AppConfig) -> bool:
    """Start the mailbox poller (idempotent). Returns True if running."""
    global _thread, _error
    with _lock:
        if _thread and _thread.is_alive():
            return True
        reason = check_settings(config)
        if reason:
            _error = reason
            logger.error("Mail poll not started: %s", reason)
            return False
        _error = None
        _stop.clear()
        _thread = threading.Thread(target=_loop, args=(config,), daemon=True, name="imap-receive")
        _thread.start()
    logger.info("Mail poll started for %s on %s (every %.0fs)", config.imap_receive.folder,
                config.imap_receive.host, config.imap_receive.poll_interval)
    return True


def stop_imap_receive() -> None:
    """Stop the poller; joins within 5 s, safe if it never started."""
    global _thread
    with _lock:
        thread = _thread
        _stop.set()
    if thread:
        thread.join(timeout=5)
        logger.info("Mail poll stopped")
    with _lock:
        if _thread is thread:
            _thread = None


def _loop(config: AppConfig) -> None:
    interval = max(0.05, float(config.imap_receive.poll_interval))
    skipped: set = set()
    while not _stop.is_set():
        try:
            poll_once(config, skipped)
        except Exception as e:
            logger.error("Mail poll error: %s", _plain(e, config))
        _stop.wait(interval)


def _add_history(filename: str, size: int, result, error: Optional[str]) -> None:
    try:
        from ..web.dashboard_api import add_to_history
        add_to_history({
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
            "size_bytes": size,
            "source": "imap_receive",
            "auto": True,
            "success": bool(result and result.ok),
            "results": result.to_dict() if result else {"ok": False, "saved_paths": [],
                                                        "errors": [error], "queued": False},
        })
    except Exception as e:
        logger.debug("Could not add mail scan to history: %s", e)
