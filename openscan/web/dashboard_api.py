"""Dashboard API endpoints."""

import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import AppConfig, save_config
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
    scanner_info = _get_scanner_info(config)
    settings = ScanSettings(source=source)

    try:
        # Execute scan
        protocol = config.scanner.protocol
        if protocol == "escl":
            from ..scanner.escl import EsclScanner
            pdf_data = EsclScanner().scan(scanner_info, settings)
        elif protocol == "wsd":
            from ..scanner.wsd import WsdScanner
            pdf_data = WsdScanner().scan(scanner_info, settings)
        else:
            handler._send_json({"error": f"Unknown protocol: {protocol}"}, status=400)
            return

        # Generate filename and save
        filename = generate_filename(config.output.filename_pattern)

        results = save_scan(
            pdf_data=pdf_data,
            output_folder=config.output.folder,
            filename=filename,
            paperless_consume=config.paperless.consume_folder if config.paperless.enabled and config.paperless.mode == "consume" else None,
            paperless_api_url=config.paperless.api_url if config.paperless.enabled and config.paperless.mode == "api" else None,
            paperless_api_token=config.paperless.api_token if config.paperless.enabled and config.paperless.mode == "api" else None,
            paperless_tags=config.paperless.default_tags if config.paperless.enabled else None,
        )

        # Add to history
        entry = {
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
            "size_bytes": len(pdf_data),
            "source": source.value,
            "results": results,
        }
        add_to_history(entry)

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
