"""Paperless-NGX destinations: the consume folder and the REST API.

Homelab edition only. An Office build deletes this file; the registry
skips it and everything else keeps working.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .base import DeliveryError, Destination, field
from .folder import save_to_folder, test_folder

logger = logging.getLogger(__name__)

PAPERLESS_TIMEOUT = 30  # seconds for a single HTTP request
PAPERLESS_TASK_WAIT = 60.0  # seconds to wait for Paperless to finish processing
PAPERLESS_TASK_POLL = 2.0

# (api_url, kind, lowercase name) -> id, kept for the life of the process
_paperless_ids: dict[tuple[str, str, str], int] = {}
_paperless_ids_lock = threading.Lock()


def save_to_paperless_consume(pdf_data: bytes, consume_folder: str, filename: str) -> str:
    """Drop the scan into the Paperless-NGX consume folder."""
    if not consume_folder:
        raise DeliveryError("No Paperless consume folder is set.")
    return save_to_folder(pdf_data, consume_folder, filename)


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


class PaperlessConsumeDestination(Destination):
    type = "paperless_consume"
    label = "Paperless-NGX consume folder"
    feature = "paperless"
    target_key = "folder"
    FIELDS = [field("folder", "Consume folder", "path", required=True)]

    def deliver(self, pdf: bytes, filename: str, options: dict) -> tuple[str, bool]:
        return save_to_paperless_consume(pdf, str(options.get("folder") or ""), filename), True

    def test(self, options: dict) -> dict:
        return test_folder(str(options.get("folder") or ""))


class PaperlessApiDestination(Destination):
    """Upload through the REST API.

    keep_local_copy=False asks the delivery loop to remove the copy saved by
    a folder destination of the same scan, once Paperless confirms it.
    """

    type = "paperless_api"
    label = "Paperless-NGX (API upload)"
    feature = "paperless"
    target_key = "url"
    FIELDS = [
        field("url", "Paperless-NGX address", "text", required=True),
        field("token", "API token", "password", required=True),
        field("tags", "Tags", "list"),
        field("correspondent", "Correspondent", "text"),
        field("document_type", "Document type", "text"),
        field("keep_local_copy", "Keep the copy in the scans folder", "bool", default=True),
    ]

    def deliver(self, pdf: bytes, filename: str, options: dict) -> tuple[str, bool]:
        url = str(options.get("url") or "")
        token = str(options.get("token") or "")
        if not url or not token:
            raise DeliveryError("Paperless-NGX address or API token is missing.")
        res = upload_to_paperless_api(
            pdf, url, token, filename, list(options.get("tags") or []),
            str(options.get("correspondent") or ""), str(options.get("document_type") or ""),
            task_wait=float(options.get("task_wait", PAPERLESS_TASK_WAIT)),
        )
        return res["location"], res["confirmed"]

    def test(self, options: dict) -> dict:
        res = test_paperless_connection(str(options.get("url") or ""), str(options.get("token") or ""))
        if res.get("ok"):
            return {"ok": True, "message": "Paperless-NGX answered."}
        return {"ok": False, "message": f"Paperless-NGX did not answer: {res.get('error', 'unknown error')}"}
