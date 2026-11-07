"""Scan audit log: one JSON line per delivered document in audit.jsonl.

On by default in the Office edition, off by default in Homelab
(config.audit.enabled). The file lives in the config folder, rotates at
MAX_BYTES and keeps KEEP old files (audit.jsonl.1 is the newest). Writing
never raises: a full disk must not lose a scan.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

FILENAME = "audit.jsonl"
MAX_BYTES = 10 * 1024 * 1024
KEEP = 5

_lock = threading.Lock()


def audit_path() -> Path:
    from .config import get_config_dir
    return get_config_dir() / FILENAME


def enabled(config) -> bool:
    audit = getattr(config, "audit", None)
    return bool(getattr(audit, "enabled", False))


def _rotate(path: Path) -> None:
    oldest = path.with_name(f"{path.name}.{KEEP}")
    if oldest.exists():
        oldest.unlink()
    for i in range(KEEP - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        if src.exists():
            os.replace(src, path.with_name(f"{path.name}.{i + 1}"))
    os.replace(path, path.with_name(f"{path.name}.1"))


def record(event: dict, path: Optional[Path] = None) -> bool:
    """Append `event` as one JSON line. Adds "time" if missing. True if written."""
    entry = dict(event)
    entry.setdefault("time", datetime.now().astimezone().isoformat(timespec="seconds"))
    line = (json.dumps(entry, ensure_ascii=False, default=str) + "\n").encode("utf-8")
    target = path or audit_path()
    with _lock:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                size = target.stat().st_size
            except FileNotFoundError:
                size = 0
            if size and size + len(line) > MAX_BYTES:
                _rotate(target)
            fd = os.open(str(target), os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
            try:
                os.write(fd, line)
            finally:
                os.close(fd)
            return True
        except OSError as e:
            logger.warning("Could not write the audit log %s: %s", target, e)
            return False


def recent(n: int = 200, path: Optional[Path] = None) -> list[dict]:
    """The last `n` events, newest first, across rotated files."""
    target = path or audit_path()
    out: list[dict] = []
    files = [target] + [target.with_name(f"{target.name}.{i}") for i in range(1, KEEP + 1)]
    with _lock:
        for f in files:
            if len(out) >= n:
                break
            try:
                lines = f.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                continue
            except OSError as e:
                logger.warning("Could not read the audit log %s: %s", f, e)
                continue
            for raw in reversed(lines):
                try:
                    item = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(item, dict):
                    out.append(item)
                    if len(out) >= n:
                        break
    return out
