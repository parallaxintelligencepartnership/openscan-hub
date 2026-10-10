"""Folder watcher scanner protocol - universal fallback.

Watches a directory for new scan files (PDF, JPEG, TIFF, PNG)
and routes them to configured output destinations.
"""

import logging
import os
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import AppConfig
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

_watcher_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()
_lock = threading.Lock()
_error: Optional[str] = None


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


def _norm(path: str) -> str:
    p = os.path.normpath(str(Path(path).expanduser().resolve()))
    if sys.platform in ("win32", "darwin"):  # case-insensitive by default
        p = p.casefold()
    return p


def _dir_ids(path: str) -> list[tuple[int, int]]:
    """(st_dev, st_ino) of the folder and each existing parent, innermost first."""
    ids = []
    p = Path(path).expanduser().absolute()
    if not p.is_dir():
        return ids  # a folder that does not exist yet has no identity
    for candidate in (p, *p.parents):
        try:
            st = os.stat(candidate)
        except OSError:
            continue
        if st.st_ino:  # 0 on file systems without inode numbers
            ids.append((st.st_dev, st.st_ino))
    return ids


def _same_or_inside_by_identity(a: str, b: str) -> bool:
    """Catch the same folder reached by two paths (bind mounts, mapped drives)."""
    ida, idb = _dir_ids(a), _dir_ids(b)
    if not ida or not idb:
        return False
    return ida[0] in idb or idb[0] in ida


def _overlaps(a: str, b: str) -> bool:
    """True if the folders are equal or one is inside the other."""
    na, nb = _norm(a), _norm(b)
    if na == nb:
        return True
    try:
        common = os.path.commonpath([na, nb])
    except ValueError:  # different drives on Windows
        common = None
    if common in (na, nb):
        return True
    return _same_or_inside_by_identity(a, b)


def check_watch_folder(config: AppConfig) -> Optional[str]:
    """Return a plain reason why this watch folder cannot be used, or None."""
    watch = config.folder_watch.watch_folder
    if not watch:
        return "Choose a folder to watch."
    if config.output.folder and _overlaps(watch, config.output.folder):
        return (
            "The watched folder cannot be the scans folder or inside it (or contain it). "
            "Otherwise every saved scan would be picked up again. Pick a different folder."
        )
    p = config.paperless
    if p.enabled and p.consume_folder and _overlaps(watch, p.consume_folder):
        return (
            "The watched folder cannot be the Paperless consume folder or inside it "
            "(or contain it). Pick a different folder."
        )
    return None


def folder_watcher_error() -> Optional[str]:
    """Why the folder watcher refused to start (None if it is fine)."""
    return _error


def folder_watcher_running() -> bool:
    return bool(_watcher_thread and _watcher_thread.is_alive())


def start_folder_watcher(config: AppConfig) -> bool:
    """Start the folder watcher (idempotent). Returns True if running."""
    global _watcher_thread, _error

    with _lock:
        if _watcher_thread and _watcher_thread.is_alive():
            return True
        reason = check_watch_folder(config)
        if reason:
            _error = reason
            logger.error("Folder watcher not started: %s", reason)
            return False
        _error = None
        _stop_event.clear()
        snapshot = _snapshot(config)
        _watcher_thread = threading.Thread(
            target=_watcher_loop, args=(config, snapshot), daemon=True, name="folder-watcher",
        )
        _watcher_thread.start()
    logger.info(
        "Folder watcher started on %s (poll every %.1fs)",
        config.folder_watch.watch_folder, config.folder_watch.poll_interval,
    )
    return True


def stop_folder_watcher() -> None:
    """Stop the watcher; joins within 5 s, safe if it never started."""
    global _watcher_thread
    with _lock:
        thread = _watcher_thread
        _stop_event.set()
    if thread:
        thread.join(timeout=5)
        logger.info("Folder watcher stopped")
    with _lock:
        if _watcher_thread is thread:
            _watcher_thread = None


def _candidates(watch_path: Path, extensions: set[str]):
    for f in watch_path.iterdir():
        if f.name.startswith(".") or f.suffix.lower() not in extensions:
            continue
        try:
            if not f.is_file():
                continue
            st = f.stat()
        except OSError:
            continue
        yield f, (st.st_size, st.st_mtime)


def _snapshot(config: AppConfig) -> dict[str, tuple]:
    """Files already in the folder when watching starts are not routed."""
    watch_path = Path(config.folder_watch.watch_folder)
    extensions = {e.lower() for e in config.folder_watch.extensions}
    try:
        if watch_path.is_dir():
            return {str(f): sig for f, sig in _candidates(watch_path, extensions)}
    except OSError as e:
        logger.warning("Could not list %s: %s", watch_path, e)
    return {}


def _watcher_loop(config: AppConfig, processed: dict[str, tuple]) -> None:
    """Poll the watch folder. A file is routed once its size and time stay
    the same across two polls (so it is not still being written)."""
    watch_path = Path(config.folder_watch.watch_folder)
    extensions = {e.lower() for e in config.folder_watch.extensions}
    interval = max(0.05, float(config.folder_watch.poll_interval))
    seen: dict[str, tuple] = {}
    logger.info("Folder watcher initialized with %d existing files", len(processed))

    while not _stop_event.is_set():
        try:
            if watch_path.is_dir():
                current = {}
                for f, sig in _candidates(watch_path, extensions):
                    key = str(f)
                    current[key] = sig
                    if processed.get(key) == sig:
                        continue
                    if seen.get(key) != sig:
                        continue  # new or still changing; check again next poll
                    logger.info("New file detected: %s", f.name)
                    _process_file(config, f)
                    processed[key] = sig
                seen = current
        except Exception as e:
            logger.error("Folder watcher error: %s", e)
        _stop_event.wait(interval)


def _process_file(config: AppConfig, file_path: Path) -> None:
    """Route a new file. The file in the watched folder is never deleted."""
    try:
        data = file_path.read_bytes()
    except OSError as e:
        logger.error("Could not read %s: %s", file_path, e)
        return
    if not data:
        return
    filename = file_path.name
    # The queue keeps its own copy, so a failed delivery survives even if the
    # user tidies up the watched folder.
    result = save_scan_for_config(config, data, filename)
    if result.ok:
        logger.info("Processed: %s (%d bytes)", filename, len(data))
    else:
        logger.error("Could not deliver %s: %s", filename, "; ".join(result.errors))

    try:
        from ..web.dashboard_api import add_to_history
        add_to_history({
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
            "size_bytes": len(data),
            "source": "folder_watch",
            "auto": True,
            "success": result.ok,
            "results": result.to_dict(),
        })
    except Exception:
        pass
