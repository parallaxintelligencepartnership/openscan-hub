"""The single path every finished scan takes, whatever its source.

    handle_scan(config, data, source=..., profile=...)
      1. ensure PDF          pages.ensure_pdf
      2. process            processing.run(profile.processing)
      3. split              splitter registered for profile.split
      4. for each document:
           name             naming.render(profile.name_rule, fields)
           deliver          destinations.deliver_all (failures -> retry queue)
           audit            audit.record, when config.audit.enabled

Scanner drivers (eSCL, WSD), the dashboard, the feeder monitor, the FTP
receiver and the folder watcher all call handle_scan. See
docs/architecture.md for the extension points.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Callable, Optional

from . import audit, destinations, naming, pages, processing, profiles
from .destinations import DeliveryResult
from .processing import ProcessResult
from .profiles import Profile

logger = logging.getLogger(__name__)

# fn(pdf, process_result) -> [(pdf, process_result), ...], one per document
Splitter = Callable[[bytes, ProcessResult], list]
_splitters: dict[str, Splitter] = {}


def register_splitter(name: str, fn: Splitter) -> None:
    """Add a splitter for Profile.split == `name`.

    It gets the processed PDF and its ProcessResult and returns one
    (pdf, ProcessResult) per document. Returning an empty list or raising
    keeps the scan as one document and adds a warning.
    """
    _splitters[name] = fn


register_splitter("none", lambda pdf, result: [(pdf, result)])


@dataclass
class DocumentResult:
    """One delivered document (a split scan has several)."""

    filename: str
    pages: int
    size_bytes: int
    delivery: DeliveryResult
    warnings: list[str] = field(default_factory=list)


@dataclass
class IntakeResult:
    """Outcome of one scan. ok/saved_paths/errors/queued cover every
    document, so it reads like a DeliveryResult."""

    ok: bool = False
    saved_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    queued: bool = False
    filename: str = ""  # name written for the first document
    size_bytes: int = 0
    page_count: int = 0
    profile_id: str = ""
    warnings: list[str] = field(default_factory=list)
    documents: list[DocumentResult] = field(default_factory=list)
    uses_source: bool = False  # the retry queue retries from source_path

    @property
    def handled(self) -> bool:
        return handled(self)

    def to_dict(self) -> dict:
        """The DeliveryResult shape the scan history stores."""
        return {"ok": self.ok, "saved_paths": list(self.saved_paths),
                "errors": list(self.errors), "queued": self.queued}


def handled(result) -> bool:
    """True when OpenScanHub owns the scan from now on: every document was
    delivered or is in the retry queue. An input may then clear its copy.

    Accepts an IntakeResult, a DeliveryResult or any object with ok/queued.
    """
    docs = getattr(result, "documents", None)
    if docs:
        return all(d.delivery.ok or d.delivery.queued for d in docs)
    return bool(getattr(result, "ok", False) or getattr(result, "queued", False))


def targets(config, profile: Profile) -> list[dict]:
    """The resolved {type, options} targets of `profile` in this edition."""
    return _targets(config, profile)


def queue_unprocessed(config, data: bytes, profile: Profile, filename: str, error: str) -> bool:
    """Put raw scan bytes in the retry queue for every target of `profile`.

    For a scan that intake could not handle at all. Returns True when queued.
    """
    items = []
    for t in _targets(config, profile):
        cls = destinations.get(t["type"])
        items.append((t["type"], cls.target(t["options"]), cls.public_options(t["options"])))
    if not items:
        return False
    from .retry_queue import get_queue
    try:
        get_queue().add(pdf_data=data, filename=filename, destinations=items, error=error)
    except Exception as e:
        logger.error("Could not queue %s for retry: %s", filename, type(e).__name__)
        return False
    return True


def _page_count(pdf: bytes) -> int:
    try:
        return pages.page_count(pdf)
    except Exception:
        return 0


_skipped: set[str] = set()


def _targets(config, profile: Profile) -> list[dict]:
    out = []
    for dest in profile.destinations:
        type_ = dest.get("type", "")
        if destinations.get(type_) is None:
            if type_ not in _skipped:
                _skipped.add(type_)
                logger.info("Destination %r is not in this edition; profile %s skips it",
                            type_, profile.name)
            continue
        out.append({"type": type_,
                    "options": destinations.resolve_options(type_, dest.get("options"), config)})
    return out


def _split(profile: Profile, pdf: bytes, result: ProcessResult) -> list[tuple[bytes, ProcessResult]]:
    mode = profile.split or "none"
    fn = _splitters.get(mode)
    if fn is None:
        result.warnings.append(f"Splitting by {mode} is not available, so the scan was saved "
                               "as one document.")
        return [(pdf, result)]
    try:
        docs = [(bytes(d), r) for d, r in (fn(pdf, result) or [])]
    except Exception as e:
        logger.exception("Splitter %s failed", mode)
        result.warnings.append(f"Splitting the scan failed, so it was saved as one document: {e}")
        return [(pdf, result)]
    return docs or [(pdf, result)]


def _given_name(filename: str, index: int, count: int, extension: str) -> str:
    base = os.path.basename(filename) or "scan"
    stem, ext = os.path.splitext(base)
    if extension:
        ext = extension
    if count > 1:
        stem = f"{stem}_{index + 1}"
    return stem + ext


def handle_scan(config, data: bytes, *, source: str, profile: Optional[Profile] = None,
                started_by: str = "", source_path: Optional[str] = None,
                source_owned: bool = False, filename: Optional[str] = None) -> IntakeResult:
    """Process, name and deliver one finished scan. Never raises for a
    delivery problem; the result says what happened.

    `source` is a short id (adf, flatbed, ftp, folder_watch, ...).
    `filename` keeps a name the file already has (FTP and folder watch);
    without it the profile's name rule makes one. `source_path` and
    `source_owned` let the retry queue use the original file instead of a
    copy while the bytes are still the original bytes.
    """
    profile = profiles.get(config) if profile is None else profiles.live(config, profile)
    now = datetime.now()
    warnings: list[str] = []
    opts = profile.processing
    if (profile.split or "none") == "blank" and opts.remove_blank_pages:
        # Blank separator sheets must reach the blank splitter, which drops them.
        opts = replace(opts, remove_blank_pages=False)
    extension = ".pdf"

    # 1. ensure PDF. A dropped file that is not a PDF stays as it is when
    # no processing is asked for, exactly as before profiles existed.
    if data.startswith(b"%PDF-"):
        pdf, convertible = data, True
    elif filename and not processing.enabled_stages(opts):
        pdf, convertible, extension = data, False, ""
    else:
        try:
            pdf, convertible = pages.ensure_pdf(data, profile.resolution), True
        except ValueError as e:
            logger.error("Could not turn the scan into a PDF: %s", e)
            warnings.append("The file is not a PDF or a picture, so it was saved unchanged.")
            pdf, convertible, extension = data, False, (os.path.splitext(filename or "")[1])

    # 2. process
    result = processing.run(pdf, opts) if convertible else ProcessResult(pdf=pdf)
    warnings.extend(result.warnings)

    # 3. split
    docs = _split(profile, result.pdf, result) if convertible else [(pdf, result)]
    for _, r in docs:
        for w in r.warnings:
            if w not in warnings:
                warnings.append(w)

    targets = _targets(config, profile)
    out = IntakeResult(profile_id=profile.id, warnings=warnings)
    for index, (doc_pdf, doc_result) in enumerate(docs):
        # 4. name
        if filename:
            name = _given_name(filename, index, len(docs), extension)
        else:
            context = {"config": config, "profile": profile, "source": source,
                       "started_by": started_by, "now": now,
                       "n": index + 1 if len(docs) > 1 else 1}
            # A profile that follows the old Settings keeps the 0.3.2 names.
            name = naming.render(profile.name_rule, naming.collect_fields(doc_result, context),
                                 legacy=profile.follow_settings)

        # 5. deliver; the queue may retry from the original file only while
        # these are still its bytes.
        use_source = bool(source_path) and len(docs) == 1 and doc_pdf == data
        delivery = destinations.deliver_all(
            doc_pdf, name, targets,
            source_path=source_path if use_source else None,
            source_owned=source_owned if use_source else False,
        )
        written = _written_name(delivery.saved_paths, name)
        doc_pages = _page_count(doc_pdf) if convertible else 0
        out.documents.append(DocumentResult(filename=written, pages=doc_pages,
                                            size_bytes=len(doc_pdf), delivery=delivery,
                                            warnings=list(doc_result.warnings)))
        out.saved_paths.extend(delivery.saved_paths)
        out.errors.extend(delivery.errors)
        out.queued = out.queued or delivery.queued
        out.uses_source = out.uses_source or (use_source and delivery.queued)
        out.size_bytes += len(doc_pdf)
        out.page_count += doc_pages

        # 6. audit
        if audit.enabled(config):
            audit.record({
                "source": source, "profile": profile.id, "profile_name": profile.name,
                "pages": doc_pages, "file": written,
                "destinations": [t["type"] for t in targets],
                "result": "ok" if delivery.ok else ("queued" if delivery.queued else "failed"),
                "errors": list(delivery.errors), "warnings": list(doc_result.warnings),
                "started_by": started_by,
            })

    out.ok = bool(out.documents) and all(d.delivery.ok for d in out.documents)
    out.filename = out.documents[0].filename if out.documents else (filename or "")
    for w in warnings:
        logger.warning("Scan %s: %s", out.filename, w)
    return out


def _written_name(saved_paths, default: str) -> str:
    """The file name actually written (the first file path, skipping URLs)."""
    for p in saved_paths or []:
        p = str(p)
        if "://" not in p:
            return os.path.basename(p)
    return default


# Registers the "blank" and "barcode" splitters and the {barcode} name field.
from . import separators  # noqa: E402,F401
