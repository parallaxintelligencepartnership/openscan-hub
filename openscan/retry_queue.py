"""Persistent retry queue for scans whose delivery failed.

Each entry is one (file, destination) pair. Entries live in
`get_config_dir()/retry_queue.json`; copies of scans that only existed in
memory live in `get_config_dir()/retry_files/`. One background thread
retries due entries with backoff 1 min, 5 min, 15 min, 1 h, then every 24 h.
After 30 days an entry is marked failed. Failed entries and their files are
kept, never deleted, so the user can still find the scan.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from .fileperm import apply_default_mode

logger = logging.getLogger(__name__)

BACKOFF = [60, 5 * 60, 15 * 60, 60 * 60]
DAILY = 24 * 60 * 60
MAX_AGE = 30 * 24 * 60 * 60
MAX_SLEEP = 30.0

STATUS_PENDING = "pending"
STATUS_FAILED = "failed"


def backoff_delay(attempts: int) -> int:
    """Seconds to wait after `attempts` failed tries (attempts >= 1)."""
    idx = max(attempts, 1) - 1
    return BACKOFF[idx] if idx < len(BACKOFF) else DAILY


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone().isoformat(timespec="seconds")


class RetryQueue:
    def __init__(self, state_path: Path, files_dir: Path, clock: Callable[[], float] = time.time):
        self.state_path = Path(state_path)
        self.files_dir = Path(files_dir)
        self.clock = clock
        self._lock = threading.RLock()
        self._entries: list[dict] = []
        self._config = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._load()

    # --- persistence ---------------------------------------------------

    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            entries = data.get("entries", []) if isinstance(data, dict) else []
            self._entries = [e for e in entries if isinstance(e, dict) and "id" in e]
        except FileNotFoundError:
            self._entries = []
        except (OSError, ValueError) as e:
            logger.error("Could not read retry queue %s: %s", self.state_path, e)
            self._entries = []

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.state_path.parent), prefix=".retry_queue.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump({"version": 1, "entries": self._entries}, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise

    # --- adding --------------------------------------------------------

    def add(self, *, filename: str, destinations: list[tuple[str, str]],
            pdf_data: Optional[bytes] = None, source_path: Optional[str] = None,
            source_owned: bool = False, paperless: Optional[dict] = None,
            error: str = "") -> list[str]:
        """Queue `filename` for each failed (destination, target). Returns ids."""
        if pdf_data is None and not source_path:
            raise ValueError("need pdf_data or source_path")
        now = self.clock()
        with self._lock:
            if pdf_data is not None:
                self.files_dir.mkdir(parents=True, exist_ok=True)
                safe = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(filename)) or "scan.pdf"
                path = self.files_dir / f"{uuid.uuid4().hex[:12]}_{safe}"
                fd, tmp = tempfile.mkstemp(dir=str(self.files_dir), suffix=".part")
                with os.fdopen(fd, "wb") as f:
                    f.write(pdf_data)
                    f.flush()
                    os.fsync(f.fileno())
                    apply_default_mode(f.fileno())
                os.replace(tmp, path)
                file_path, owned = str(path), True
            else:
                file_path, owned = str(source_path), bool(source_owned)
            ids = []
            for destination, target in destinations:
                entry = {
                    "id": uuid.uuid4().hex[:12],
                    "file": file_path,
                    "filename": os.path.basename(filename),
                    "destination": destination,
                    "target": target,
                    "owns_file": owned,
                    "paperless": dict(paperless or {}),
                    "attempts": 1,
                    "last_error": error,
                    "created": now,
                    "next_try_ts": now + backoff_delay(1),
                    "status": STATUS_PENDING,
                }
                entry["next_try"] = _iso(entry["next_try_ts"])
                self._entries.append(entry)
                ids.append(entry["id"])
            self._save()
        logger.warning("Queued %s for retry (%s)", filename, ", ".join(d for d, _ in destinations))
        self._wake.set()
        return ids

    # --- reading -------------------------------------------------------

    def pending(self) -> list[dict]:
        """Every undelivered entry (status pending or failed), oldest first."""
        keys = ("id", "file", "filename", "destination", "attempts", "last_error", "next_try", "status")
        with self._lock:
            return [{k: e.get(k) for k in keys} for e in self._entries]

    # --- retrying ------------------------------------------------------

    def _attempt(self, entry: dict) -> bool:
        """Try one entry once. Caller holds no lock while delivering."""
        from .output import DeliveryError, deliver_to

        try:
            data = Path(entry["file"]).read_bytes()
        except OSError as e:
            return self._record(entry, False, f"The saved copy of this scan is missing: {e}")
        cfg = self._config
        token = ""
        if cfg is not None:
            token = getattr(cfg.paperless, "api_token", "") or ""
        p = entry.get("paperless") or {}
        try:
            deliver_to(entry["destination"], entry["target"], data, entry["filename"],
                       api_token=token, tags=p.get("tags"), correspondent=p.get("correspondent", ""),
                       document_type=p.get("document_type", ""))
        except DeliveryError as e:
            return self._record(entry, False, str(e))
        except Exception as e:  # never let one entry kill the thread
            logger.exception("Retry of %s crashed", entry.get("filename"))
            return self._record(entry, False, str(e))
        return self._record(entry, True, "")

    def _record(self, entry: dict, ok: bool, error: str) -> bool:
        now = self.clock()
        with self._lock:
            current = next((e for e in self._entries if e["id"] == entry["id"]), None)
            if current is None:
                return ok
            if ok:
                self._entries.remove(current)
                logger.info("Retry delivered %s to %s", current["filename"], current["destination"])
                if current.get("owns_file") and not any(e["file"] == current["file"] for e in self._entries):
                    try:
                        os.unlink(current["file"])
                    except OSError:
                        pass
            else:
                current["attempts"] = int(current.get("attempts", 0)) + 1
                current["last_error"] = error
                if now - float(current.get("created", now)) >= MAX_AGE:
                    current["status"] = STATUS_FAILED
                    current["next_try_ts"] = None
                    current["next_try"] = None
                    logger.error("Giving up on %s after 30 days: %s", current["filename"], error)
                else:
                    current["next_try_ts"] = now + backoff_delay(current["attempts"])
                    current["next_try"] = _iso(current["next_try_ts"])
            self._save()
        return ok

    def retry_now(self, entry_id: str) -> bool:
        """Retry one entry immediately (also works on failed entries)."""
        with self._lock:
            entry = next((dict(e) for e in self._entries if e["id"] == entry_id), None)
        if entry is None:
            return False
        return self._attempt(entry)

    def retry_all(self) -> int:
        """Retry every entry now. Returns how many were delivered."""
        with self._lock:
            ids = [e["id"] for e in self._entries]
        return sum(1 for i in ids if self.retry_now(i))

    def run_due(self) -> int:
        """Retry entries whose time has come. Returns how many were delivered."""
        now = self.clock()
        with self._lock:
            due = [dict(e) for e in self._entries
                   if e.get("status") == STATUS_PENDING and (e.get("next_try_ts") or 0) <= now]
        return sum(1 for e in due if not self._stop.is_set() and self._attempt(e))

    def _seconds_until_next(self) -> float:
        now = self.clock()
        with self._lock:
            times = [e["next_try_ts"] for e in self._entries
                     if e.get("status") == STATUS_PENDING and e.get("next_try_ts")]
        if not times:
            return MAX_SLEEP
        return max(0.0, min(MAX_SLEEP, min(times) - now))

    # --- thread --------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_due()
            except Exception:
                logger.exception("Retry queue error")
            self._wake.wait(self._seconds_until_next())
            self._wake.clear()

    def start(self, config) -> None:
        """Start the background thread (idempotent). Also refreshes config."""
        with self._lock:
            self._config = config
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="retry-queue", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        with self._lock:
            thread = self._thread
            self._stop.set()
            self._wake.set()
        if thread:
            thread.join(timeout=5)
        with self._lock:
            self._thread = None

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())


_queue: Optional[RetryQueue] = None
_queue_lock = threading.Lock()


def get_queue() -> RetryQueue:
    global _queue
    with _queue_lock:
        if _queue is None:
            from .config import get_config_dir
            base = get_config_dir()
            _queue = RetryQueue(base / "retry_queue.json", base / "retry_files")
        return _queue


def _reset_queue_for_tests() -> None:
    global _queue
    with _queue_lock:
        if _queue is not None:
            _queue.stop()
        _queue = None
