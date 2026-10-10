"""Delivery of finished scans to their destinations.

The local folder (which can be a NAS share) is the primary destination and
works on its own. Paperless-NGX is optional. A scan is never thrown away when
a destination fails: the failure is handed to the retry queue together with a
copy of the file, and the caller gets a DeliveryResult that says so.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

if TYPE_CHECKING:  # pragma: no cover
    from .config import AppConfig

from .fileperm import apply_default_mode

logger = logging.getLogger(__name__)

# Serializes "pick a free name, then move the file there" across threads
# (FTP uploads, folder watch and manual scans can all save at once).
_name_lock = threading.Lock()

PAPERLESS_TIMEOUT = 30  # seconds for a single HTTP request
PAPERLESS_TASK_WAIT = 60.0  # seconds to wait for Paperless to finish processing
PAPERLESS_TASK_POLL = 2.0

DEST_FOLDER = "folder"
DEST_CONSUME = "paperless_consume"
DEST_API = "paperless_api"


@dataclass
class DeliveryResult:
    """Outcome of delivering one scan to every enabled destination."""

    ok: bool = False
    saved_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    queued: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class DeliveryError(Exception):
    """A destination failed; the message is meant for the user."""


# --- Filenames -----------------------------------------------------------

_COUNTER_RE = re.compile(r"^(.*_)(\d{3})$")


def generate_filename(pattern: str, extension: str = ".pdf", folder: Optional[str] = None) -> str:
    """Build a filename from a pattern with {date}, {time} and {n}.

    {n} becomes a three digit counter. When `folder` is given the first free
    number in that folder is used; otherwise it starts at 001 and the save
    step moves it up if that name is taken.
    """
    now = datetime.now()
    name = pattern.replace("{date}", now.strftime("%Y-%m-%d"))
    name = name.replace("{time}", now.strftime("%H%M%S"))
    if not name.lower().endswith(extension.lower()):
        name_ext = extension
    else:
        name_ext = ""

    if "{n}" in name:
        n = 1
        if folder:
            base = Path(folder)
            while n < 100000 and (base / (name.replace("{n}", str(n).zfill(3)) + name_ext)).exists():
                n += 1
        name = name.replace("{n}", str(n).zfill(3))

    return name + name_ext


def _unique_path(folder: Path, filename: str) -> Path:
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


def written_name(saved_paths, default: str) -> str:
    """File name actually written: the first saved file path, skipping
    Paperless document URLs. `default` when only Paperless received it."""
    for p in saved_paths or []:
        p = str(p)
        if "://" not in p:
            return os.path.basename(p)
    return default


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
            final = _unique_path(folder_path, os.path.basename(filename))
            os.replace(tmp_name, final)
        tmp_name = None
    except OSError as e:
        msg = folder_error_message(folder, e)
        logger.error("Failed to save to %s: %s", folder, e)
        raise DeliveryError(msg) from e
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    logger.info("Saved scan to %s (%d bytes)", final, len(pdf_data))
    return str(final)


def save_to_paperless_consume(pdf_data: bytes, consume_folder: str, filename: str) -> str:
    """Drop the scan into the Paperless-NGX consume folder."""
    if not consume_folder:
        raise DeliveryError("No Paperless consume folder is set.")
    return save_to_folder(pdf_data, consume_folder, filename)


# --- Paperless-NGX API ---------------------------------------------------

# (api_url, kind, lowercase name) -> id, kept for the life of the process
_paperless_ids: dict[tuple[str, str, str], int] = {}
_paperless_ids_lock = threading.Lock()


def _paperless_request(api_url: str, token: str, path: str, data: Optional[bytes] = None,
                       content_type: Optional[str] = None, method: Optional[str] = None,
                       timeout: float = PAPERLESS_TIMEOUT):
    url = f"{api_url.rstrip('/')}{path}"
    headers = {"Authorization": f"Token {token}", "Accept": "application/json"}
    if content_type:
        headers["Content-Type"] = content_type
    req = Request(url, data=data, method=method or ("POST" if data is not None else "GET"), headers=headers)
    with urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw) if raw else None


def _paperless_error(e: Exception) -> str:
    if isinstance(e, HTTPError):
        if e.code in (401, 403):
            return "Paperless-NGX refused the API token. Check the token in Settings."
        try:
            detail = e.read().decode("utf-8", errors="replace")[:200]
        except Exception:
            detail = ""
        return f"Paperless-NGX returned error {e.code}. {detail}".strip()
    if isinstance(e, (URLError, OSError)):
        return "Could not reach Paperless-NGX. Check that it is running and the address is correct."
    return f"Paperless-NGX upload failed: {e}"


def resolve_paperless_id(api_url: str, token: str, kind: str, name: str, create: bool = False) -> Optional[int]:
    """Look up the id of a tag, correspondent or document type by name.

    `kind` is "tags", "correspondents" or "document_types". Only tags are
    ever created (create=True); a missing correspondent or document type
    returns None. Results are cached for the life of the process.
    """
    name = name.strip()
    if not name:
        return None
    key = (api_url.rstrip("/"), kind, name.lower())
    with _paperless_ids_lock:
        if key in _paperless_ids:
            return _paperless_ids[key]
    data = _paperless_request(api_url, token, f"/api/{kind}/?name__iexact={quote(name)}")
    results = (data or {}).get("results") or []
    found = None
    for item in results:
        if str(item.get("name", "")).lower() == name.lower():
            found = int(item["id"])
            break
    if found is None and create:
        # matching_algorithm 0 = none, so the new tag is never auto-assigned
        # to unrelated documents by Paperless.
        body = json.dumps({"name": name, "matching_algorithm": 0}).encode()
        created = _paperless_request(api_url, token, f"/api/{kind}/", data=body,
                                     content_type="application/json")
        found = int(created["id"])
        logger.info("Created Paperless tag %r (id %d)", name, found)
    if found is not None:
        with _paperless_ids_lock:
            _paperless_ids[key] = found
    return found


def _multipart(fields: list[tuple[str, str]], filename: str, pdf_data: bytes) -> tuple[bytes, str]:
    boundary = "OpenScanHub" + secrets.token_hex(16)
    while boundary.encode() in pdf_data:
        boundary = "OpenScanHub" + secrets.token_hex(16)
    safe_name = re.sub(r'[\r\n"\\]', "_", os.path.basename(filename)) or "scan.pdf"
    ctype = "application/pdf" if safe_name.lower().endswith(".pdf") else "application/octet-stream"
    body = bytearray()
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="document"; filename="{safe_name}"\r\n'.encode()
    body += f"Content-Type: {ctype}\r\n\r\n".encode()
    body += pdf_data + b"\r\n"
    for name, value in fields:
        body += f"--{boundary}\r\n".encode()
        body += f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
        body += value.encode("utf-8") + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return bytes(body), f"multipart/form-data; boundary={boundary}"


def upload_to_paperless_api(
    pdf_data: bytes,
    api_url: str,
    api_token: str,
    filename: str,
    tags: Optional[list[str]] = None,
    correspondent: str = "",
    document_type: str = "",
    task_wait: float = PAPERLESS_TASK_WAIT,
) -> dict:
    """Upload a scan to Paperless-NGX and wait for it to be processed.

    Returns {"confirmed": bool, "document_id": int|None, "location": str}.
    "confirmed" is True when Paperless reported success within `task_wait`
    seconds. Raises DeliveryError when the upload or processing failed.
    """
    try:
        fields: list[tuple[str, str]] = [("title", Path(filename).stem)]
        for tag in tags or []:
            tag_id = resolve_paperless_id(api_url, api_token, "tags", tag, create=True)
            if tag_id is not None:
                fields.append(("tags", str(tag_id)))
        if correspondent:
            cid = resolve_paperless_id(api_url, api_token, "correspondents", correspondent)
            if cid is None:
                logger.warning("Paperless correspondent %r not found; uploading without it", correspondent)
            else:
                fields.append(("correspondent", str(cid)))
        if document_type:
            did = resolve_paperless_id(api_url, api_token, "document_types", document_type)
            if did is None:
                logger.warning("Paperless document type %r not found; uploading without it", document_type)
            else:
                fields.append(("document_type", str(did)))

        body, ctype = _multipart(fields, filename, pdf_data)
        task_id = _paperless_request(api_url, api_token, "/api/documents/post_document/",
                                     data=body, content_type=ctype)
    except DeliveryError:
        raise
    except Exception as e:  # network, HTTP, bad JSON
        logger.error("Paperless upload failed: %s", e)
        raise DeliveryError(_paperless_error(e)) from e

    logger.info("Uploaded %s to Paperless (task %s)", filename, task_id)
    location = f"{api_url.rstrip('/')}/documents/"
    if not isinstance(task_id, str) or task_wait <= 0:
        return {"confirmed": False, "document_id": None, "location": location}

    deadline = time.monotonic() + task_wait
    while True:
        try:
            tasks = _paperless_request(api_url, api_token, f"/api/tasks/?task_id={quote(task_id)}",
                                       timeout=min(PAPERLESS_TIMEOUT, max(1.0, task_wait)))
        except Exception as e:
            logger.warning("Could not check Paperless task %s: %s", task_id, e)
            tasks = None
        if isinstance(tasks, dict):
            tasks = tasks.get("results")
        task = tasks[0] if isinstance(tasks, list) and tasks else None
        status = (task or {}).get("status", "")
        if status == "SUCCESS":
            doc_id = task.get("related_document")
            if doc_id:
                location = f"{api_url.rstrip('/')}/documents/{doc_id}/details"
            return {"confirmed": True, "document_id": doc_id, "location": location}
        if status in ("FAILURE", "REVOKED"):
            reason = str(task.get("result") or "unknown reason")[:200]
            raise DeliveryError(f"Paperless-NGX could not process the document: {reason}")
        if time.monotonic() >= deadline:
            logger.info("Paperless task %s still running after %.0fs", task_id, task_wait)
            return {"confirmed": False, "document_id": None, "location": location}
        time.sleep(min(PAPERLESS_TASK_POLL, max(0.05, deadline - time.monotonic())))


def test_paperless_connection(api_url: str, api_token: str) -> dict:
    """Test connection to Paperless-NGX API. Returns status dict."""
    url = f"{api_url.rstrip('/')}/api/"
    headers = {"Authorization": f"Token {api_token}"}

    try:
        req = Request(url, method="GET", headers=headers)
        with urlopen(req, timeout=10) as resp:
            return {"ok": True, "status": resp.status}
    except HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}", "status": e.code}
    except (URLError, OSError) as e:
        return {"ok": False, "error": str(e)}


test_paperless_connection.__test__ = False  # not a pytest test


# --- Delivery ------------------------------------------------------------

def deliver_to(destination: str, target: str, pdf_data: bytes, filename: str, *,
               api_token: str = "", tags: Optional[list[str]] = None, correspondent: str = "",
               document_type: str = "", task_wait: float = PAPERLESS_TASK_WAIT) -> tuple[str, bool]:
    """Deliver to one destination. Returns (saved location, confirmed).

    Raises DeliveryError on failure. Used by save_scan and the retry queue.
    """
    if destination == DEST_FOLDER:
        return save_to_folder(pdf_data, target, filename), True
    if destination == DEST_CONSUME:
        return save_to_paperless_consume(pdf_data, target, filename), True
    if destination == DEST_API:
        if not target or not api_token:
            raise DeliveryError("Paperless-NGX address or API token is missing.")
        res = upload_to_paperless_api(pdf_data, target, api_token, filename, tags,
                                      correspondent, document_type, task_wait=task_wait)
        return res["location"], res["confirmed"]
    raise DeliveryError(f"Unknown destination {destination!r}")


def save_scan(
    pdf_data: bytes,
    output_folder: str,
    filename: str,
    paperless_consume: Optional[str] = None,
    paperless_api_url: Optional[str] = None,
    paperless_api_token: Optional[str] = None,
    paperless_tags: Optional[list[str]] = None,
    *,
    paperless_correspondent: str = "",
    paperless_document_type: str = "",
    keep_local_copy: bool = True,
    source_path: Optional[str] = None,
    source_owned: bool = False,
    queue_failures: bool = True,
    task_wait: float = PAPERLESS_TASK_WAIT,
) -> DeliveryResult:
    """Deliver a scan to every enabled destination.

    Destinations: the scans folder (if set), the Paperless consume folder (if
    given) and the Paperless API (if address and token given). Every failed
    destination goes to the retry queue. If `source_path` is given, the queue
    retries from that file (and deletes it once delivered when
    `source_owned`); otherwise the queue keeps its own copy of the bytes.

    With keep_local_copy=False and the Paperless API in use, the copy in the
    scans folder is removed only after Paperless confirms the document.
    """
    result = DeliveryResult()
    failures: list[tuple[str, str]] = []  # (destination, target)
    local_path: Optional[str] = None

    plan: list[tuple[str, str]] = []
    if output_folder:
        plan.append((DEST_FOLDER, output_folder))
    if paperless_consume:
        plan.append((DEST_CONSUME, paperless_consume))
    if paperless_api_url and paperless_api_token:
        plan.append((DEST_API, paperless_api_url))

    if not plan:
        result.errors.append("No destination is set up. Choose a scans folder in Settings.")
        return result

    api_confirmed = False
    for destination, target in plan:
        try:
            location, confirmed = deliver_to(
                destination, target, pdf_data, filename,
                api_token=paperless_api_token or "", tags=paperless_tags,
                correspondent=paperless_correspondent, document_type=paperless_document_type,
                task_wait=task_wait,
            )
        except DeliveryError as e:
            result.errors.append(str(e))
            failures.append((destination, target))
            continue
        result.saved_paths.append(location)
        if destination == DEST_FOLDER:
            local_path = location
        if destination == DEST_API:
            api_confirmed = confirmed

    if (not keep_local_copy and local_path and api_confirmed and not failures):
        try:
            os.unlink(local_path)
            result.saved_paths.remove(local_path)
            logger.info("Removed local copy %s (Paperless confirmed it)", local_path)
        except OSError as e:
            logger.warning("Could not remove local copy %s: %s", local_path, e)

    if failures and queue_failures:
        try:
            from .retry_queue import get_queue
            get_queue().add(
                pdf_data=None if source_path else pdf_data,
                source_path=source_path,
                source_owned=source_owned,
                filename=filename,
                destinations=failures,
                paperless={
                    "tags": list(paperless_tags or []),
                    "correspondent": paperless_correspondent,
                    "document_type": paperless_document_type,
                },
                error="; ".join(result.errors),
            )
            result.queued = True
        except Exception as e:
            logger.error("Could not queue %s for retry: %s", filename, e)

    result.ok = not failures
    return result


def save_scan_for_config(config: "AppConfig", pdf_data: bytes, filename: str, *,
                         source_path: Optional[str] = None, source_owned: bool = False) -> DeliveryResult:
    """save_scan with every destination and Paperless option taken from config."""
    p = config.paperless
    use_consume = p.enabled and p.mode == "consume"
    use_api = p.enabled and p.mode == "api"
    return save_scan(
        pdf_data=pdf_data,
        output_folder=config.output.folder,
        filename=filename,
        paperless_consume=p.consume_folder if use_consume else None,
        paperless_api_url=p.api_url if use_api else None,
        paperless_api_token=p.api_token if use_api else None,
        paperless_tags=p.tags if p.enabled else None,
        paperless_correspondent=p.correspondent if use_api else "",
        paperless_document_type=p.document_type if use_api else "",
        keep_local_copy=p.keep_local_copy if use_api else True,
        source_path=source_path,
        source_owned=source_owned,
    )
