"""Logging: rotating log file, console when there is one, and a ring buffer of
recent problems for the dashboard (review finding I14)."""

import collections
import logging
import logging.handlers
import sys
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
DATEFMT = "%Y-%m-%d %H:%M:%S"
LOG_FILE_NAME = "openscan.log"
MAX_BYTES = 5 * 1024 * 1024
BACKUP_COUNT = 3
RING_SIZE = 200

_handlers: list[logging.Handler] = []
_log_dir: Optional[Path] = None
_setup_lock = threading.Lock()


class _RingBufferHandler(logging.Handler):
    """Keeps the last RING_SIZE records at WARNING and above."""

    def __init__(self, capacity: int = RING_SIZE):
        super().__init__(level=logging.WARNING)
        self.records: collections.deque = collections.deque(maxlen=capacity)
        self._lock_ring = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
        except Exception:
            message = str(record.msg)
        if record.exc_info and record.exc_info[1] is not None:
            message = f"{message} ({record.exc_info[1]})"
        entry = {
            "time": datetime.fromtimestamp(record.created).isoformat(timespec="seconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": message,
        }
        with self._lock_ring:
            self.records.append(entry)

    def snapshot(self, limit: int) -> list[dict]:
        with self._lock_ring:
            items = list(self.records)
        items.reverse()  # newest first
        return items[: max(0, limit)]

    def clear(self) -> None:
        with self._lock_ring:
            self.records.clear()


_ring = _RingBufferHandler()


def _level_from(config, level: Optional[str]) -> int:
    name = level or getattr(config, "log_level", None) or "INFO"
    return getattr(logging, str(name).upper(), logging.INFO)


def _pick_log_dir() -> Optional[Path]:
    from .config import get_config_dir

    candidates = [get_config_dir() / "logs", Path(tempfile.gettempdir()) / "OpenScanHub" / "logs"]
    for candidate in candidates:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            probe = candidate / ".write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return candidate
        except OSError:
            continue
    return None


def setup_logging(config=None, level: Optional[str] = None) -> Optional[Path]:
    """Configure root logging. Safe to call again (e.g. after the config loads).

    Returns the log folder, or None if no folder was writable.
    """
    global _log_dir
    with _setup_lock:
        root = logging.getLogger()
        for handler in _handlers:
            root.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass
        _handlers.clear()

        formatter = logging.Formatter(FORMAT, DATEFMT)
        root.setLevel(_level_from(config, level))

        _log_dir = _pick_log_dir()
        if _log_dir is not None:
            try:
                fh = logging.handlers.RotatingFileHandler(
                    _log_dir / LOG_FILE_NAME, maxBytes=MAX_BYTES,
                    backupCount=BACKUP_COUNT, encoding="utf-8",
                )
                fh.setFormatter(formatter)
                _handlers.append(fh)
            except OSError:
                _log_dir = None

        # A windowed (PyInstaller --windowed) build has no stderr.
        if sys.stderr is not None:
            ch = logging.StreamHandler(sys.stderr)
            ch.setFormatter(formatter)
            _handlers.append(ch)

        _handlers.append(_ring)
        for handler in _handlers:
            root.addHandler(handler)

        # Quiet chatty libraries unless debugging.
        if root.level > logging.DEBUG:
            for noisy in ("pyftpdlib", "zeroconf"):
                logging.getLogger(noisy).setLevel(logging.WARNING)
        return _log_dir


def get_log_dir() -> Optional[Path]:
    """Folder that holds openscan.log (None before setup or if unwritable)."""
    return _log_dir


def recent_errors(limit: int = 50) -> list[dict]:
    """Most recent WARNING+ records, newest first. Keys: time, level, logger, message."""
    return _ring.snapshot(limit)


def clear_recent_errors() -> None:
    _ring.clear()
