"""FTP receiver scanner protocol - Scan to FTP replacement.

Runs a lightweight FTP server that receives files pushed by scanners
configured with "Scan to FTP", then routes them through the standard
output pipeline (folders, Paperless-NGX, etc.).

This replaces the need for a standalone FTP server. Just point your
scanner's Scan-to-FTP settings at OpenScanHub's IP on port 2121.
"""

import logging
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import AppConfig
from ..output import save_scan
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


def start_ftp_receiver(config: AppConfig) -> None:
    """Start the FTP receiver server in a daemon thread."""
    global _ftp_thread

    if _ftp_thread and _ftp_thread.is_alive():
        logger.warning("FTP receiver already running")
        return

    _ftp_thread = threading.Thread(
        target=_run_ftp_server,
        args=(config,),
        daemon=True,
        name="ftp-receiver",
    )
    _ftp_thread.start()


def stop_ftp_receiver() -> None:
    """Stop the FTP receiver server."""
    global _ftp_server
    if _ftp_server is not None:
        _ftp_server.close_all()
        _ftp_server = None
    logger.info("FTP receiver stopped")


def _run_ftp_server(config: AppConfig) -> None:
    """Run the FTP server."""
    global _ftp_server

    try:
        from pyftpdlib.authorizers import DummyAuthorizer
        from pyftpdlib.handlers import FTPHandler
        from pyftpdlib.servers import FTPServer
    except ImportError:
        logger.error(
            "pyftpdlib not installed. Install it with: pip install pyftpdlib"
        )
        return

    ftp_port = config.ftp_receive.port
    ftp_user = config.ftp_receive.username
    ftp_pass = config.ftp_receive.password

    # Create a staging directory for incoming files
    staging_dir = Path(config.ftp_receive.staging_dir)
    staging_dir.mkdir(parents=True, exist_ok=True)

    # Set up FTP authorizer
    authorizer = DummyAuthorizer()
    if ftp_user and ftp_pass:
        authorizer.add_user(ftp_user, ftp_pass, str(staging_dir), perm="elradfmw")
    else:
        # Anonymous access (common for scanner FTP configs)
        authorizer.add_anonymous(str(staging_dir), perm="elradfmw")

    # Custom handler that routes files after upload
    class ScanFTPHandler(FTPHandler):
        def on_file_received(self, file_path):
            """Called when a file upload completes."""
            logger.info("FTP file received: %s", file_path)
            threading.Thread(
                target=_process_received_file,
                args=(config, Path(file_path)),
                daemon=True,
            ).start()

    ScanFTPHandler.authorizer = authorizer
    ScanFTPHandler.passive_ports = range(60000, 60100)
    ScanFTPHandler.banner = "OpenScanHub FTP Receiver ready."

    _ftp_server = FTPServer(("0.0.0.0", ftp_port), ScanFTPHandler)
    _ftp_server.max_cons = 10
    _ftp_server.max_cons_per_ip = 5

    logger.info(
        "FTP receiver started on port %d (user: %s)",
        ftp_port,
        ftp_user or "anonymous",
    )

    try:
        _ftp_server.serve_forever()
    except Exception as e:
        logger.error("FTP server error: %s", e)
    finally:
        _ftp_server = None


def _process_received_file(config: AppConfig, file_path: Path) -> None:
    """Process a file received via FTP - route to configured outputs."""
    valid_extensions = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}

    if file_path.suffix.lower() not in valid_extensions:
        logger.debug("Ignoring non-scan file: %s", file_path.name)
        return

    # Wait briefly to ensure file is fully written
    time.sleep(0.5)

    try:
        data = file_path.read_bytes()
        if not data:
            logger.warning("Empty file received: %s", file_path.name)
            return

        filename = file_path.name
        logger.info("Processing FTP scan: %s (%d bytes)", filename, len(data))

        results = save_scan(
            pdf_data=data,
            output_folder=config.output.folder,
            filename=filename,
            paperless_consume=(
                config.paperless.consume_folder
                if config.paperless.enabled and config.paperless.mode == "consume"
                else None
            ),
            paperless_api_url=(
                config.paperless.api_url
                if config.paperless.enabled and config.paperless.mode == "api"
                else None
            ),
            paperless_api_token=(
                config.paperless.api_token
                if config.paperless.enabled and config.paperless.mode == "api"
                else None
            ),
            paperless_tags=(
                config.paperless.default_tags
                if config.paperless.enabled
                else None
            ),
        )

        logger.info("FTP scan routed: %s -> %s", filename, results.get("saved", []))

        # Add to dashboard history
        try:
            from ..web.dashboard_api import add_to_history
            add_to_history({
                "filename": filename,
                "timestamp": datetime.now().isoformat(),
                "size_bytes": len(data),
                "source": "ftp",
                "auto": True,
                "results": results,
            })
        except ImportError:
            pass

        # Clean up staging file after successful processing
        if config.ftp_receive.delete_after_routing:
            file_path.unlink(missing_ok=True)

    except Exception as e:
        logger.error("Failed to process FTP file %s: %s", file_path.name, e)
