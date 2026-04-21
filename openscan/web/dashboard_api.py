"""Dashboard API endpoints."""

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import AppConfig, save_config
from .. import multipage
from ..output import generate_filename, save_scan
from ..scanner.base import ScannerInfo, ScanSettings, ScanSource
from .server import route, get_config

logger = logging.getLogger(__name__)

# Track recent scans in memory
_scan_history: list[dict] = []
_scan_lock = threading.Lock()
_scan_in_progress = False

MAX_HISTORY = 50


def add_to_history(entry: dict) -> None:
    """Add a scan result to the history."""
    with _scan_lock:
        _scan_history.insert(0, entry)
        if len(_scan_history) > MAX_HISTORY:
            _scan_history.pop()


@route("GET", "/api/status")
def api_status(handler, query):
    """Get current scanner status."""
    config = get_config()

    if not config.scanner.ip:
        handler._send_json({
            "configured": False,
            "error": "No scanner configured",
        })
        return

    scanner_info = _get_scanner_info(config)
    protocol = config.scanner.protocol

    try:
        if protocol == "escl":
            from ..scanner.escl import EsclScanner
            status = EsclScanner().get_status(scanner_info)
        elif protocol == "wsd":
            from ..scanner.wsd import WsdScanner
            status = WsdScanner().get_status(scanner_info)
        else:
            handler._send_json({"configured": True, "status": {"state": "Unknown"}})
            return

        handler._send_json({
            "configured": True,
            "scanner": scanner_info.to_dict(),
            "status": status.to_dict(),
            "scan_in_progress": _scan_in_progress,
        })
    except Exception as e:
        handler._send_json({
            "configured": True,
            "scanner": scanner_info.to_dict(),
            "status": {"state": "Offline"},
            "error": str(e),
        })


@route("GET", "/api/scan/adf")
def api_scan_adf(handler, query):
    """Trigger an ADF scan."""
    _do_scan(handler, ScanSource.ADF)


@route("GET", "/api/scan/flatbed")
def api_scan_flatbed(handler, query):
    """Trigger a flatbed scan."""
    _do_scan(handler, ScanSource.FLATBED)


@route("POST", "/api/scan")
def api_scan(handler, query):
    """Trigger a scan with custom settings."""
    body = handler.read_json_body()
    source_str = body.get("source", "Platen")
    source = ScanSource.ADF if source_str == "Feeder" else ScanSource.FLATBED
    _do_scan(handler, source)


@route("GET", "/api/history")
def api_history(handler, query):
    """Get recent scan history."""
    limit = int(query.get("limit", [20])[0])
    with _scan_lock:
        handler._send_json({
            "scans": _scan_history[:limit],
            "total": len(_scan_history),
        })


@route("GET", "/api/config")
def api_get_config(handler, query):
    """Get current config (with secrets redacted)."""
    config = get_config()
    handler._send_json({
        "scanner": {
            "ip": config.scanner.ip,
            "port": config.scanner.port,
            "protocol": config.scanner.protocol,
            "name": config.scanner.name,
            "model": config.scanner.model,
        },
        "output": {
            "folder": config.output.folder,
            "filename_pattern": config.output.filename_pattern,
        },
        "paperless": {
            "enabled": config.paperless.enabled,
            "mode": config.paperless.mode,
            "api_url": config.paperless.api_url,
            "has_token": bool(config.paperless.api_token),
        },
        "monitor": {
            "enabled": config.monitor.enabled,
            "poll_interval": config.monitor.poll_interval,
        },
        "wizard_completed": config.wizard_completed,
    })


def _do_scan(handler, source: ScanSource) -> None:
    """Execute a scan and save results."""
    global _scan_in_progress

    if _scan_in_progress:
        handler._send_json({"error": "Scan already in progress"}, status=409)
        return

    config = get_config()
    if not config.scanner.ip:
        handler._send_json({"error": "No scanner configured"}, status=400)
        return

    _scan_in_progress = True

    try:
        pdf_data = _execute_scan(source)
        filename = generate_filename(config.output.filename_pattern)
        results = _save_pdf(pdf_data, filename)

        add_to_history({
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
            "size_bytes": len(pdf_data),
            "source": source.value,
            "results": results,
        })

        handler._send_json({
            "success": True,
            "filename": filename,
            "size_bytes": len(pdf_data),
            "results": results,
        })

    except Exception as e:
        logger.exception("Scan failed")
        handler._send_json({"success": False, "error": str(e)}, status=500)
    finally:
        _scan_in_progress = False


def _get_scanner_info(config: AppConfig) -> ScannerInfo:
    """Build ScannerInfo from config."""
    return ScannerInfo(
        name=config.scanner.name,
        ip=config.scanner.ip,
        port=config.scanner.port,
        protocol=config.scanner.protocol,
        model=config.scanner.model,
    )


def _execute_scan(source: ScanSource) -> bytes:
    """Execute scan and return PDF data."""
    config = get_config()
    scanner_info = _get_scanner_info(config)
    settings = ScanSettings(source=source)

    protocol = config.scanner.protocol
    if protocol == "escl":
        from ..scanner.escl import EsclScanner
        scan_data = EsclScanner().scan(scanner_info, settings)
    elif protocol == "wsd":
        from ..scanner.wsd import WsdScanner
        scan_data = WsdScanner().scan(scanner_info, settings)
    else:
        raise ValueError(f"Unknown protocol: {protocol}")

    return multipage.ensure_pdf(scan_data)


def _save_pdf(pdf_data: bytes, filename: str) -> dict:
    """Save PDF with paperless integration."""
    config = get_config()
    return save_scan(
        pdf_data=pdf_data,
        output_folder=config.output.folder,
        filename=filename,
        paperless_consume=config.paperless.consume_folder if config.paperless.enabled and config.paperless.mode == "consume" else None,
        paperless_api_url=config.paperless.api_url if config.paperless.enabled and config.paperless.mode == "api" else None,
        paperless_api_token=config.paperless.api_token if config.paperless.enabled and config.paperless.mode == "api" else None,
        paperless_tags=config.paperless.default_tags if config.paperless.enabled else None,
    )


# --- Multi-page scanning endpoints ---

@route("POST", "/api/multipage/start")
def api_multipage_start(handler, query):
    """Start a new multi-page scanning session."""
    body = handler.read_json_body()
    source_str = body.get("source", "Platen")
    source = ScanSource.ADF if source_str == "Feeder" else ScanSource.FLATBED
    session_id = multipage.create_session(source)
    handler._send_json({"session_id": session_id})


@route("POST", "/api/multipage/scan")
def api_multipage_scan(handler, query):
    """Scan a page and add to multi-page session."""
    global _scan_in_progress

    body = handler.read_json_body()
    session_id = body.get("session_id")

    if not session_id:
        handler._send_json({"error": "Missing session_id"}, status=400)
        return

    session = multipage.get_session(session_id)
    if not session:
        handler._send_json({"error": "Session not found or expired"}, status=404)
        return

    if _scan_in_progress:
        handler._send_json({"error": "Scan already in progress"}, status=409)
        return

    config = get_config()
    if not config.scanner.ip:
        handler._send_json({"error": "No scanner configured"}, status=400)
        return

    _scan_in_progress = True

    try:
        scan_data = _execute_scan(session.source)
        page_count = multipage.add_page(session_id, scan_data)
        handler._send_json({
            "success": True,
            "page_count": page_count,
            "page_index": page_count - 1,
            "size_bytes": len(scan_data),
        })
    except Exception as e:
        logger.exception("Multipage scan failed")
        handler._send_json({"success": False, "error": str(e)}, status=500)
    finally:
        _scan_in_progress = False


@route("GET", "/api/multipage/thumbnail/")
def api_multipage_thumbnail(handler, query, path_suffix=""):
    """Get JPEG thumbnail for a page in a multi-page session."""
    # path_suffix is "session_id/page_index"
    parts = path_suffix.strip("/").split("/")
    if len(parts) != 2:
        handler._send_json({"error": "Invalid path"}, status=400)
        return

    session_id = parts[0]
    try:
        page_index = int(parts[1])
    except ValueError:
        handler._send_json({"error": "Invalid page index"}, status=400)
        return

    try:
        jpeg_bytes = multipage.get_page_thumbnail(session_id, page_index)
        handler.send_response(200)
        handler.send_header("Content-Type", "image/jpeg")
        handler.send_header("Content-Length", str(len(jpeg_bytes)))
        handler.send_header("Cache-Control", "max-age=300")
        handler.end_headers()
        handler.wfile.write(jpeg_bytes)
    except ValueError as e:
        handler._send_json({"error": str(e)}, status=404)
    except IndexError as e:
        handler._send_json({"error": str(e)}, status=404)
    except Exception as e:
        logger.exception("Thumbnail generation failed")
        handler._send_json({"error": str(e)}, status=500)


@route("POST", "/api/multipage/save")
def api_multipage_save(handler, query):
    """Merge pages and save the multi-page document."""
    body = handler.read_json_body()
    session_id = body.get("session_id")

    if not session_id:
        handler._send_json({"error": "Missing session_id"}, status=400)
        return

    session = multipage.get_session(session_id)
    if not session:
        handler._send_json({"error": "Session not found or expired"}, status=404)
        return

    if not session.pages:
        handler._send_json({"error": "No pages to save"}, status=400)
        return

    config = get_config()

    try:
        page_count = len(session.pages)
        pdf_data = multipage.merge_pages(session_id)
        filename = generate_filename(config.output.filename_pattern)
        results = _save_pdf(pdf_data, filename)

        add_to_history({
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
            "size_bytes": len(pdf_data),
            "source": session.source.value,
            "page_count": page_count,
            "results": results,
        })

        multipage.delete_session(session_id)

        handler._send_json({
            "success": True,
            "filename": filename,
            "page_count": page_count,
            "size_bytes": len(pdf_data),
            "results": results,
        })
    except Exception as e:
        logger.exception("Failed to save multipage document")
        handler._send_json({"success": False, "error": str(e)}, status=500)


@route("POST", "/api/multipage/cancel")
def api_multipage_cancel(handler, query):
    """Cancel and discard a multi-page session."""
    body = handler.read_json_body()
    session_id = body.get("session_id")

    if not session_id:
        handler._send_json({"error": "Missing session_id"}, status=400)
        return

    multipage.delete_session(session_id)
    handler._send_json({"success": True})


@route("GET", "/api/multipage/status/")
def api_multipage_status(handler, query, path_suffix=""):
    """Get status of a multi-page session."""
    session_id = path_suffix.strip("/")
    if not session_id:
        handler._send_json({"error": "Missing session_id"}, status=400)
        return

    info = multipage.get_session_info(session_id)

    if not info:
        handler._send_json({"error": "Session not found or expired"}, status=404)
        return

    handler._send_json(info)
