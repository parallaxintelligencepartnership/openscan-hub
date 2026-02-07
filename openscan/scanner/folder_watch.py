"""Folder watcher scanner protocol - universal fallback.

Watches a directory for new scan files (PDF, JPEG, TIFF, PNG)
and routes them to configured output destinations.
"""

import logging
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

from ..config import AppConfig
from ..output import save_to_folder, save_to_paperless_consume, upload_to_paperless_api
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

_watcher_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


class FolderWatcher(ScannerProtocol):
    """Folder watcher as a scanner protocol.

    Watches a directory for new files and treats them as 'scans'.
    This is the universal fallback for scanners that don't support
    eSCL or WSD but can save to a network folder.
    """

    def discover(self, timeout: float = 5.0) -> list[ScannerInfo]:
        """Folder watchers are configured manually, not discovered."""
        return []

    def get_status(self, scanner: ScannerInfo) -> ScannerStatus:
        """Check if the watched folder exists and is accessible."""
        folder = scanner.ip  # We store folder path in IP field
        if Path(folder).is_dir():
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
        """Read the most recent file from the watched folder."""
        folder = Path(scanner.ip)
        if not folder.is_dir():
            raise FileNotFoundError(f"Watch folder not found: {folder}")

        extensions = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}
        files = sorted(
            (f for f in folder.iterdir() if f.suffix.lower() in extensions),
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )

        if not files:
            raise FileNotFoundError("No scan files found in watched folder")

        latest = files[0]
        data = latest.read_bytes()
        logger.info("Read %s from watch folder (%d bytes)", latest.name, len(data))
        return data

    def test_connection(self, scanner: ScannerInfo) -> bool:
        folder = Path(scanner.ip)
        return folder.is_dir()


def start_folder_watcher(config: AppConfig) -> None:
    """Start the folder watcher daemon thread."""
    global _watcher_thread

    if not config.folder_watch.watch_folder:
        logger.warning("No watch folder configured")
        return

    if _watcher_thread and _watcher_thread.is_alive():
        logger.warning("Folder watcher already running")
        return

    _stop_event.clear()
    _watcher_thread = threading.Thread(
        target=_watcher_loop,
        args=(config,),
        daemon=True,
        name="folder-watcher",
    )
    _watcher_thread.start()
    logger.info(
        "Folder watcher started on %s (poll every %.1fs)",
        config.folder_watch.watch_folder,
        config.folder_watch.poll_interval,
    )


def stop_folder_watcher() -> None:
    """Stop the folder watcher thread."""
    _stop_event.set()
    if _watcher_thread:
        _watcher_thread.join(timeout=5)
    logger.info("Folder watcher stopped")


def _watcher_loop(config: AppConfig) -> None:
    """Main watcher loop - detects new files in the watch folder."""
    watch_path = Path(config.folder_watch.watch_folder)
    extensions = set(config.folder_watch.extensions)
    processed: dict[str, float] = {}  # filename -> mtime

    # Build initial snapshot to avoid processing existing files
    if watch_path.is_dir():
        for f in watch_path.iterdir():
            if f.suffix.lower() in extensions and f.is_file():
                processed[str(f)] = f.stat().st_mtime

    logger.info("Folder watcher initialized with %d existing files", len(processed))

    while not _stop_event.is_set():
        if not watch_path.is_dir():
            _stop_event.wait(config.folder_watch.poll_interval)
            continue

        try:
            for f in watch_path.iterdir():
                if not f.is_file():
                    continue
                if f.suffix.lower() not in extensions:
                    continue

                file_key = str(f)
                mtime = f.stat().st_mtime

                if file_key in processed and processed[file_key] == mtime:
                    continue

                # Wait for file to stabilize (not still being written)
                time.sleep(1.0)
                new_mtime = f.stat().st_mtime
                if new_mtime != mtime:
                    # File still being written, skip this cycle
                    continue

                logger.info("New file detected: %s", f.name)
                _process_file(config, f)
                processed[file_key] = mtime

        except Exception as e:
            logger.error("Folder watcher error: %s", e)

        _stop_event.wait(config.folder_watch.poll_interval)


def _process_file(config: AppConfig, file_path: Path) -> None:
    """Process a newly detected file - copy to output destinations."""
    data = file_path.read_bytes()
    filename = file_path.name

    # Save to output folder
    if config.output.folder:
        save_to_folder(data, config.output.folder, filename)

    # Save to Paperless consume folder
    if config.paperless.enabled and config.paperless.mode == "consume":
        save_to_paperless_consume(data, config.paperless.consume_folder, filename)

    # Upload to Paperless API
    if config.paperless.enabled and config.paperless.mode == "api":
        upload_to_paperless_api(
            data,
            config.paperless.api_url,
            config.paperless.api_token,
            filename,
            config.paperless.default_tags,
        )

    logger.info("Processed: %s (%d bytes)", filename, len(data))

    # Add to dashboard history
    try:
        from ..web.dashboard_api import add_to_history
        from datetime import datetime
        add_to_history({
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
            "size_bytes": len(data),
            "source": "folder_watch",
            "auto": True,
        })
    except ImportError:
        pass
