"""Setup wizard API endpoints."""

import logging
import platform
from pathlib import Path

from ..config import _is_docker
from ..discovery import discover_all, probe_scanner
from ..output import test_paperless_connection
from ..scanner.base import ScannerInfo, ScanSettings, release_scan, try_acquire_scan
from .dashboard_api import BUSY_MESSAGE, clear_caps_cache, parse_source
from .server import BadRequest, get_config, route
from .settings_api import commit_config, ensure_folder, merge_config

logger = logging.getLogger(__name__)

DISCOVER_MIN, DISCOVER_MAX = 1.0, 15.0


def default_output_folder() -> str:
    """Where scans go unless the user picks something else."""
    if _is_docker():
        return "/scans"
    if platform.system() == "Windows":
        return str(Path.home() / "Documents" / "Scans")
    return str(Path.home() / "Scans")


def lan_ip() -> str:
    from ..scanner.ftp_receive import primary_ipv4
    return primary_ipv4()


def _int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _scanner_from(body: dict) -> ScannerInfo:
    return ScannerInfo(
        name=str(body.get("name", "")),
        ip=str(body.get("ip", "")),
        port=_int(body.get("port", 80), 80),
        protocol=str(body.get("protocol", "escl")),
        model=str(body.get("model", "")),
        path=str(body.get("path", "")),
    )


def _impl(protocol: str):
    if protocol == "escl":
        from ..scanner.escl import EsclScanner
        return EsclScanner()
    if protocol == "wsd":
        from ..scanner.wsd import WsdScanner
        return WsdScanner()
    raise BadRequest(f"Unknown protocol: {protocol}")


@route("GET", "/api/defaults")
def api_defaults(handler, query):
    cfg = get_config()
    handler._send_json({
        "output_folder": cfg.output.folder or default_output_folder(),
        "default_output_folder": default_output_folder(),
        "platform": platform.system(),
        "is_docker": _is_docker(),
        "lan_ip": lan_ip(),
        "ftp_address": _ftp_address(cfg),
    })


def _ftp_address(cfg) -> str:
    from ..scanner.ftp_receive import passive_address
    return passive_address(cfg)


@route("POST", "/api/discover")
def api_discover(handler, query):
    """Search the network for scanners. Body: {"timeout": seconds (1 to 15)}."""
    body = handler.read_json_body()
    try:
        timeout = float(body.get("timeout", 5))
    except (TypeError, ValueError):
        timeout = 5.0
    timeout = max(DISCOVER_MIN, min(DISCOVER_MAX, timeout))
    scanners = discover_all(timeout=timeout)
    found = [s.to_dict() for s in scanners]
    handler._send_json({"scanners": found, "count": len(found)})


@route("POST", "/api/probe")
def api_probe(handler, query):
    """Check one address for a scanner (manual entry)."""
    body = handler.read_json_body()
    ip = str(body.get("ip", "")).strip()
    if not ip:
        raise BadRequest("Please enter the scanner's IP address")
    protocol = str(body.get("protocol", "escl"))
    scanner = probe_scanner(ip, _int(body.get("port", 80), 80), protocol)
    if scanner:
        handler._send_json({"scanner": scanner.to_dict(), "found": True})
    else:
        handler._send_json({"found": False, "error": "No scanner answered at that address"})


@route("POST", "/api/test-scanner")
def api_test_scanner(handler, query):
    """Check that a scanner answers and report what it can do."""
    body = handler.read_json_body()
    info = _scanner_from(body)
    impl = _impl(info.protocol)
    if not impl.test_connection(info):
        handler._send_json({"connected": False, "error": "Cannot reach the scanner"})
        return
    data = {"connected": True, "capabilities": impl.get_capabilities(info).to_dict()}
    try:
        data["status"] = impl.get_status(info).to_dict()
    except Exception as e:
        logger.debug("Status check failed: %s", e)
    handler._send_json(data)


@route("POST", "/api/test-scan")
def api_test_scan(handler, query):
    """Scan one low-resolution page from the given scanner without saving it."""
    body = handler.read_json_body()
    info = _scanner_from(body)
    impl = _impl(info.protocol)
    settings = ScanSettings(source=parse_source(body.get("source", "flatbed")), resolution=150)
    if not try_acquire_scan(timeout=0):
        handler._send_json({"success": False, "error": BUSY_MESSAGE}, status=409)
        return
    try:
        data = impl.scan(info, settings)
    except Exception as e:
        logger.warning("Test scan failed: %s", e)
        handler._send_json({"success": False, "error": str(e)})
        return
    finally:
        release_scan()
    handler._send_json({"success": True, "size_bytes": len(data), "size_readable": _format_size(len(data))})


@route("POST", "/api/test-paperless")
def api_test_paperless(handler, query):
    """Test a Paperless-NGX address. A blank token means "use the saved one"."""
    body = handler.read_json_body()
    url = str(body.get("url", "")).strip()
    token = str(body.get("token", "")).strip() or get_config().paperless.api_token
    if not url:
        raise BadRequest("Please enter the Paperless-NGX address")
    handler._send_json(test_paperless_connection(url, token))


@route("POST", "/api/save-config")
def api_save_config(handler, query):
    """Save the wizard. Merges into the existing config; never wipes other settings."""
    body = handler.read_json_body()
    old = get_config()
    new = merge_config(old, body)
    if not new.output.folder:
        raise BadRequest("Please choose a folder for your scans")
    problem = ensure_folder(new.output.folder)
    if problem:
        raise BadRequest(problem)
    new.wizard_completed = True
    clear_caps_cache()
    report = commit_config(old, new)
    handler._send_json({
        "saved": True,
        "restart_needed": report["restart_needed"],
        "warnings": report["errors"],
    })


@route("GET", "/api/wizard-status")
def api_wizard_status(handler, query):
    config = get_config()
    handler._send_json({
        "wizard_completed": config.wizard_completed,
        "has_scanner": bool(config.scanner.ip),
        "has_output": bool(config.output.folder),
    })


def _format_size(size_bytes: float) -> str:
    for unit in ("B", "KB", "MB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} GB"
