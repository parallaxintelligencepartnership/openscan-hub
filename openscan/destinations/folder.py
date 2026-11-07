"""Folder destination: a local folder or a mounted NAS share."""

from __future__ import annotations

import errno
import logging
import os
import re
import tempfile
import threading
from pathlib import Path

from ..fileperm import apply_default_mode
from .base import DeliveryError, Destination, field

logger = logging.getLogger(__name__)

# Serializes "pick a free name, then move the file there" across threads
# (FTP uploads, folder watch and manual scans can all save at once).
_name_lock = threading.Lock()

_COUNTER_RE = re.compile(r"^(.*_)(\d{3})$")


def unique_path(folder: Path, filename: str) -> Path:
    """Return a path in `folder` that does not exist yet.

    A name ending in a three digit counter (scan_..._001.pdf) counts up
    (002, 003, ...). Any other name gets _2, _3, ... appended.
    """
    path = folder / filename
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    m = _COUNTER_RE.match(stem)
    if m:
        prefix, start = m.group(1), int(m.group(2)) + 1
        for n in range(start, start + 100000):
            candidate = folder / f"{prefix}{str(n).zfill(3)}{suffix}"
            if not candidate.exists():
                return candidate
    counter = 2
    while True:
        candidate = folder / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _is_network_path(folder: str) -> bool:
    return folder.startswith("\\\\") or folder.startswith("//")


_UNREACHABLE_ERRNOS = {
    getattr(errno, name)
    for name in ("EHOSTDOWN", "EHOSTUNREACH", "ENETUNREACH", "ENETDOWN", "ETIMEDOUT", "ESTALE", "ENOTCONN")
    if hasattr(errno, name)
}
# Windows: 53 network path not found, 64 network name deleted, 67 bad net name
_UNREACHABLE_WINERRORS = {53, 64, 67, 1231}


def folder_error_message(folder: str, exc: OSError) -> str:
    """Turn an OSError into a plain message a non-technical user can act on."""
    winerror = getattr(exc, "winerror", None)
    unreachable = (
        f"Could not reach {folder}. Check that the share is online and the path is correct."
    )
    if exc.errno in _UNREACHABLE_ERRNOS or winerror in _UNREACHABLE_WINERRORS:
        return unreachable
    if isinstance(exc, PermissionError):
        return f"OpenScanHub is not allowed to write to {folder}. Check the folder permissions."
    if exc.errno == errno.ENOSPC:
        return f"There is no free space left in {folder}."
    if _is_network_path(folder):
        return unreachable
    reason = exc.strerror or str(exc)
    return f"Could not save to {folder}: {reason}."


def save_to_folder(pdf_data: bytes, folder: str, filename: str) -> str:
    """Write `pdf_data` into `folder` atomically and return the final path.

    The data is written to a hidden temporary file in the same folder and
    then moved into place, so a half-written scan never appears under its
    real name. Raises DeliveryError with a plain message on failure.
    """
    if not folder:
        raise DeliveryError("No scans folder is set.")
    folder_path = Path(folder)
    tmp_name = None
    try:
        folder_path.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=str(folder_path), prefix=".openscan-", suffix=".part")
        with os.fdopen(fd, "wb") as f:
            f.write(pdf_data)
            f.flush()
            os.fsync(f.fileno())
            apply_default_mode(f.fileno())
        with _name_lock:
            final = unique_path(folder_path, os.path.basename(filename))
            os.replace(tmp_name, final)
        tmp_name = None
    except OSError as e:
        msg = folder_error_message(folder, e)
        logger.error("Failed to save to %s: %s", folder, e)
        raise DeliveryError(msg) from e
    except ValueError as e:  # for example a NUL character in the path
        logger.error("Failed to save to a folder: %s", type(e).__name__)
        raise DeliveryError("The folder path is not valid. Choose the folder again.") from None
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    logger.info("Saved scan to %s (%d bytes)", final, len(pdf_data))
    return str(final)


def test_folder(folder: str) -> dict:
    """Create and remove a test file in `folder`. Returns {ok, message}."""
    if not folder:
        return {"ok": False, "message": "No folder is set."}
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=folder, prefix=".openscan-test-", suffix=".tmp")
        os.close(fd)
        os.unlink(name)
    except OSError as e:
        return {"ok": False, "message": folder_error_message(folder, e)}
    except ValueError:
        return {"ok": False, "message": "The folder path is not valid."}
    return {"ok": True, "message": f"OpenScanHub can write to {folder}."}


test_folder.__test__ = False  # not a pytest test


class FolderDestination(Destination):
    type = "folder"
    label = "Folder or network share"
    feature = None
    target_key = "folder"
    FIELDS = [field("folder", "Folder", "path", required=True)]

    def deliver(self, pdf: bytes, filename: str, options: dict) -> tuple[str, bool]:
        return save_to_folder(pdf, str(options.get("folder") or ""), filename), True

    def test(self, options: dict) -> dict:
        return test_folder(str(options.get("folder") or ""))
