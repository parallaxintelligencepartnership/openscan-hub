"""Delivery of finished scans to their destinations (pre-0.4 interface).

The destinations themselves live in `openscan.destinations`; new code goes
through `openscan.intake.handle_scan`. This module keeps the old public
functions working as thin wrappers so existing callers and tests do not
change. A scan is never thrown away when a destination fails: the failure
is handed to the retry queue together with a copy of the file.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:  # pragma: no cover
    from .config import AppConfig

from .destinations import DeliveryError, DeliveryResult, deliver_all, deliver_one
from .destinations.folder import (  # noqa: F401  (re-exported)
    _name_lock,
    folder_error_message,
    save_to_folder,
    unique_path as _unique_path,
)

try:  # an Office build has no Paperless module
    from .destinations.paperless import (  # noqa: F401  (re-exported)
        PAPERLESS_TASK_POLL,
        PAPERLESS_TASK_WAIT,
        PAPERLESS_TIMEOUT,
        _multipart,
        _paperless_error,
        _paperless_ids,
        _paperless_request,
        resolve_paperless_id,
        save_to_paperless_consume,
        test_paperless_connection,
        upload_to_paperless_api,
    )
except ImportError:  # pragma: no cover - exercised only in Office builds
    PAPERLESS_TIMEOUT = 30
    PAPERLESS_TASK_WAIT = 60.0
    PAPERLESS_TASK_POLL = 2.0
    _paperless_ids: dict = {}

    def _unavailable(*args, **kwargs):
        raise DeliveryError("Paperless-NGX is not available in this edition.")

    save_to_paperless_consume = upload_to_paperless_api = resolve_paperless_id = _unavailable

    def test_paperless_connection(api_url: str, api_token: str) -> dict:
        return {"ok": False, "error": "Paperless-NGX is not available in this edition."}

    test_paperless_connection.__test__ = False

DEST_FOLDER = "folder"
DEST_CONSUME = "paperless_consume"
DEST_API = "paperless_api"


# --- Filenames -----------------------------------------------------------


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


def written_name(saved_paths, default: str) -> str:
    """File name actually written: the first saved file path, skipping
    Paperless document URLs. `default` when only Paperless received it."""
    for p in saved_paths or []:
        p = str(p)
        if "://" not in p:
            return os.path.basename(p)
    return default


def _target_options(destination: str, target: str, *, api_token: str = "",
                    tags: Optional[list[str]] = None, correspondent: str = "",
                    document_type: str = "", task_wait: float = PAPERLESS_TASK_WAIT,
                    keep_local_copy: bool = True) -> dict:
    if destination == DEST_API:
        return {"url": target, "token": api_token, "tags": list(tags or []),
                "correspondent": correspondent, "document_type": document_type,
                "keep_local_copy": keep_local_copy, "task_wait": task_wait}
    return {"folder": target}


def deliver_to(destination: str, target: str, pdf_data: bytes, filename: str, *,
               api_token: str = "", tags: Optional[list[str]] = None, correspondent: str = "",
               document_type: str = "", task_wait: float = PAPERLESS_TASK_WAIT) -> tuple[str, bool]:
    """Deliver to one destination. Returns (saved location, confirmed).

    Raises DeliveryError on failure. Wrapper over destinations.deliver_one.
    """
    options = _target_options(destination, target, api_token=api_token, tags=tags,
                              correspondent=correspondent, document_type=document_type,
                              task_wait=task_wait)
    return deliver_one(destination, pdf_data, filename, options)


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
    given) and the Paperless API (if address and token given). Wrapper over
    destinations.deliver_all; see there for the queue and local-copy rules.
    """
    targets: list[dict] = []
    if output_folder:
        targets.append({"type": DEST_FOLDER, "options": {"folder": output_folder}})
    if paperless_consume:
        targets.append({"type": DEST_CONSUME, "options": {"folder": paperless_consume}})
    if paperless_api_url and paperless_api_token:
        targets.append({"type": DEST_API, "options": _target_options(
            DEST_API, paperless_api_url, api_token=paperless_api_token, tags=paperless_tags,
            correspondent=paperless_correspondent, document_type=paperless_document_type,
            task_wait=task_wait, keep_local_copy=keep_local_copy)})
    return deliver_all(pdf_data, filename, targets, source_path=source_path,
                       source_owned=source_owned, queue_failures=queue_failures)


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
