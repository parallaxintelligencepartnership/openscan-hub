"""FTP receiver scanner protocol - Scan to FTP replacement.

Runs a lightweight FTP server that receives files pushed by scanners
configured with "Scan to FTP", then routes them through the standard
output pipeline (folders, Paperless-NGX, etc.).

This replaces the need for a standalone FTP server. Just point your
scanner's Scan-to-FTP settings at OpenScanHub's IP on port 2121.
"""

import errno
import logging
import os
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import AppConfig, get_config_dir
from ..output import save_scan_for_config
from .base import (
    ScannerCapabilities,
    ScannerInfo,
    ScannerProtocol,
    ScannerState,
    ScannerStatus,
    ScanSettings,
    ScanSource,
)

logger = logging.getLogger(__name__)

_ftp_thread: Optional[threading.Thread] = None
_ftp_server = None
_ftp_stop = threading.Event()
_ftp_lock = threading.Lock()
_ftp_error: Optional[str] = None

SCAN_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}
# e = change directory, l = list, w = upload. No read, delete, rename or mkdir.
FTP_PERMS = "elw"
RECEIVED_DIR = "received"


class FtpReceiver(ScannerProtocol):
    """FTP receiver as a scanner protocol.

    Instead of OpenScanHub pulling from a scanner, the scanner pushes
    files to OpenScanHub's built-in FTP server. This is a drop-in
    replacement for Scan-to-FTP on any scanner that supports it.
    """

    def discover(self, timeout: float = 5.0) -> list[ScannerInfo]:
        """FTP receivers are configured manually, not discovered."""
        return []

    def get_status(self, scanner: ScannerInfo) -> ScannerStatus:
        """Check if the FTP server is running."""
        if _ftp_server is not None:
            return ScannerStatus(state=ScannerState.IDLE)
        return ScannerStatus(state=ScannerState.OFFLINE)

    def get_capabilities(self, scanner: ScannerInfo) -> ScannerCapabilities:
        return ScannerCapabilities(
            sources=[ScanSource.FLATBED],
            formats=["application/pdf", "image/jpeg", "image/png", "image/tiff"],
        )

    def scan(
        self,
        scanner: ScannerInfo,
        settings: Optional[ScanSettings] = None,
    ) -> bytes:
        """FTP receiver doesn't initiate scans - the scanner pushes to us."""
        raise NotImplementedError(
            "FTP receiver is passive - the scanner pushes files to us. "
            "Configure your scanner's Scan-to-FTP to point at this server."
        )

    def test_connection(self, scanner: ScannerInfo) -> bool:
        return _ftp_server is not None


def sanitize_filename(name: str, default: str = "scan") -> str:
    """Basename only, no control or reserved characters, sane length."""
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = re.sub(r'[<>:"|?*]', "_", name).strip().rstrip(". ")
    if name.startswith("."):
        name = default + name
    if len(name) > 150:
        stem, dot, ext = name.rpartition(".")
        name = (stem[: 150 - len(ext) - 1] + "." + ext) if dot and len(ext) <= 10 else name[:150]
    return name or default


def _unique_in(folder: Path, name: str) -> Path:
    path = folder / name
    stem, suffix = path.stem, path.suffix
    n = 2
    while path.exists():
        path = folder / f"{stem}_{n}{suffix}"
        n += 1
    return path


def parse_passive_ports(spec: str) -> Optional[range]:
    """'60000-60010' -> range(60000, 60011). Returns None if invalid."""
    m = re.fullmatch(r"\s*(\d{1,5})\s*-\s*(\d{1,5})\s*", spec or "")
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2))
    if not (1024 <= lo <= hi <= 65535):
        return None
    return range(lo, hi + 1)


def ftp_receiver_running() -> bool:
    return bool(_ftp_thread and _ftp_thread.is_alive())


def ftp_receiver_error() -> Optional[str]:
    """Why the receiver is not running, in plain words (None if fine)."""
    return _ftp_error


def ftp_receiver_address() -> Optional[tuple[str, int]]:
    server = _ftp_server
    if server is None:
        return None
    return server.socket.getsockname()[:2]


def _build_server(config: AppConfig, host: str = "0.0.0.0"):
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import FTPHandler
    from pyftpdlib.servers import FTPServer

    fc = config.ftp_receive
    staging_dir = Path(fc.staging_dir or (get_config_dir() / "ftp_staging"))
    staging_dir.mkdir(parents=True, exist_ok=True)
    (staging_dir / RECEIVED_DIR).mkdir(exist_ok=True)

    authorizer = DummyAuthorizer()
    if fc.username and fc.password:
        authorizer.add_user(fc.username, fc.password, str(staging_dir), perm=FTP_PERMS)
    if fc.allow_anonymous:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # pyftpdlib warns about anonymous write
            authorizer.add_anonymous(str(staging_dir), perm=FTP_PERMS)
    if not authorizer.user_table:
        raise ValueError("Set an FTP username and password in Settings.")

    passive = parse_passive_ports(fc.passive_ports)
    if passive is None:
        logger.warning("Invalid FTP passive port range %r; using 60000-60010", fc.passive_ports)
        passive = range(60000, 60011)

    class ScanFTPHandler(FTPHandler):
        def on_file_received(self, file):
            # Move the upload out of the way at once so a second upload with
            # the same name cannot overwrite it before it is routed.
            src = Path(file)
            name = sanitize_filename(src.name)
            dest_dir = staging_dir / RECEIVED_DIR
            try:
                with _ftp_lock:
                    dest = _unique_in(dest_dir, name)
                    os.replace(src, dest)
            except OSError as e:
                logger.error("Could not move FTP upload %s: %s", src, e)
                return
            logger.info("FTP file received: %s", dest.name)
            threading.Thread(
                target=_process_received_file, args=(config, dest), daemon=True,
                name="ftp-route",
            ).start()

        def on_incomplete_file_received(self, file):
            logger.warning("Incomplete FTP upload discarded: %s", os.path.basename(file))
            try:
                os.unlink(file)
            except OSError:
                pass

    ScanFTPHandler.authorizer = authorizer
    ScanFTPHandler.passive_ports = passive
    ScanFTPHandler.banner = "OpenScanHub FTP receiver ready."
    ScanFTPHandler.permit_foreign_addresses = False

    server = FTPServer((host, fc.port), ScanFTPHandler)
    server.max_cons = 10
    server.max_cons_per_ip = 5
    return server


def start_ftp_receiver(config: AppConfig, host: str = "0.0.0.0") -> bool:
    """Start the FTP receiver (idempotent). Returns True if it is running.

    It listens on all interfaces because the scanner has to push files to it
    over the LAN; that is why it only runs while enabled in Settings.
    """
    global _ftp_thread, _ftp_server, _ftp_error
    with _ftp_lock:
        if _ftp_thread and _ftp_thread.is_alive():
            return True
        try:
            server = _build_server(config, host)
        except ImportError:
            _ftp_error = "the pyftpdlib package is not installed"
            logger.error("FTP receiver could not start: %s", _ftp_error)
            return False
        except OSError as e:
            if e.errno is None and e.args and isinstance(e.args[0], OSError):
                e = e.args[0]  # pyftpdlib wraps the bind error
            if e.errno in (errno.EADDRINUSE, getattr(errno, "WSAEADDRINUSE", -1)):
                _ftp_error = f"port {config.ftp_receive.port} is in use by another program"
            else:
                _ftp_error = e.strerror or str(e)
            logger.error("FTP receiver could not start: %s", _ftp_error)
            return False
        except ValueError as e:
            _ftp_error = str(e)
            logger.error("FTP receiver could not start: %s", _ftp_error)
            return False
        _ftp_error = None
        _ftp_server = server
        _ftp_stop.clear()
        _ftp_thread = threading.Thread(target=_serve, args=(server,), daemon=True, name="ftp-receiver")
        _ftp_thread.start()
    logger.info(
        "FTP receiver started on port %d (user: %s%s)",
        server.socket.getsockname()[1], config.ftp_receive.username or "none",
        ", anonymous allowed" if config.ftp_receive.allow_anonymous else "",
    )
    return True


def _serve(server) -> None:
    global _ftp_server
    try:
        while not _ftp_stop.is_set():
            server.ioloop.loop(timeout=0.2, blocking=False)
    except Exception as e:
        logger.error("FTP server error: %s", e)
    finally:
        try:
            server.close_all()
        except Exception:
            pass
        if _ftp_server is server:
            _ftp_server = None


def stop_ftp_receiver() -> None:
    """Stop the receiver; joins within 5 s, safe if it never started."""
    global _ftp_thread
    with _ftp_lock:
        thread = _ftp_thread
        _ftp_stop.set()
    if thread:
        thread.join(timeout=5)
        logger.info("FTP receiver stopped")
    with _ftp_lock:
        if _ftp_thread is thread:
            _ftp_thread = None


def _process_received_file(config: AppConfig, file_path: Path) -> None:
    """Route a received file. It is deleted only after every destination
    succeeded; otherwise it stays in staging and the retry queue owns it."""
    if file_path.suffix.lower() not in SCAN_EXTENSIONS:
        logger.warning("Ignoring non-scan file from FTP (kept in staging): %s", file_path.name)
        return
    try:
        data = file_path.read_bytes()
    except OSError as e:
        logger.error("Could not read FTP file %s: %s", file_path.name, e)
        return
    if not data:
        logger.warning("Empty file received over FTP: %s", file_path.name)
        return

    filename = file_path.name
    delete = bool(config.ftp_receive.delete_after_routing)
    try:
        result = save_scan_for_config(config, data, filename, source_path=str(file_path),
                                      source_owned=delete)
    except Exception as e:  # never lose the staged file on a bug
        logger.exception("Failed to route FTP file %s", filename)
        _add_history(filename, len(data), None, str(e))
        return

    if result.ok:
        logger.info("FTP scan routed: %s -> %s", filename, result.saved_paths)
        if delete:
            try:
                file_path.unlink()
            except OSError:
                pass
    else:
        logger.error("FTP scan %s not fully delivered (%s); kept at %s",
                     filename, "; ".join(result.errors), file_path)
    _add_history(filename, len(data), result, None)


def _add_history(filename: str, size: int, result, error: Optional[str]) -> None:
    try:
        from ..web.dashboard_api import add_to_history
    except ImportError:
        return
    entry = {
        "filename": filename,
        "timestamp": datetime.now().isoformat(),
        "size_bytes": size,
        "source": "ftp",
        "auto": True,
        "success": bool(result and result.ok),
        "results": result.to_dict() if result else {"ok": False, "saved_paths": [], "errors": [error],
                                                     "queued": False},
    }
    try:
        add_to_history(entry)
    except Exception as e:
        logger.debug("Could not add FTP scan to history: %s", e)
