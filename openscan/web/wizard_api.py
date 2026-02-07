"""Setup wizard API endpoints."""

import logging
import threading

from ..config import (
    AppConfig, ScannerConfig, OutputConfig, PaperlessConfig,
    MonitorConfig, FtpReceiveConfig, save_config, load_config,
)
from ..discovery import discover_all, probe_scanner
from ..output import test_paperless_connection
from ..scanner.base import ScanSettings, ScanSource, ScannerInfo
from .server import route, get_config, set_config

logger = logging.getLogger(__name__)

# Cache discovery results during wizard flow
_discovery_cache: list[dict] = []
_discovery_lock = threading.Lock()


@route("GET", "/api/discover")
def api_discover(handler, query):
    """Run scanner discovery and return found scanners."""
    global _discovery_cache
    timeout = float(query.get("timeout", [5])[0])

    scanners = discover_all(timeout=timeout)

    with _discovery_lock:
        _discovery_cache = [s.to_dict() for s in scanners]

    handler._send_json({
        "scanners": _discovery_cache,
        "count": len(scanners),
    })


@route("POST", "/api/probe")
def api_probe(handler, query):
    """Probe a specific IP for a scanner (manual entry)."""
    body = handler.read_json_body()
    ip = body.get("ip", "")
    port = int(body.get("port", 80))
    protocol = body.get("protocol", "escl")

    if not ip:
        handler._send_json({"error": "IP address required"}, status=400)
        return

    scanner = probe_scanner(ip, port, protocol)
    if scanner:
        handler._send_json({"scanner": scanner.to_dict(), "found": True})
    else:
        handler._send_json({"found": False, "error": "No scanner found at that address"})


@route("POST", "/api/test-scanner")
def api_test_scanner(handler, query):
    """Test connectivity to a specific scanner."""
    body = handler.read_json_body()

    scanner_info = ScannerInfo(
        name=body.get("name", ""),
        ip=body.get("ip", ""),
        port=int(body.get("port", 80)),
        protocol=body.get("protocol", "escl"),
        model=body.get("model", ""),
    )

    protocol = body.get("protocol", "escl")
    if protocol == "escl":
        from ..scanner.escl import EsclScanner
        escl = EsclScanner()
        connected = escl.test_connection(scanner_info)
        if connected:
            caps = escl.get_capabilities(scanner_info)
            status = escl.get_status(scanner_info)
            handler._send_json({
                "connected": True,
                "capabilities": caps.to_dict(),
                "status": status.to_dict(),
            })
        else:
            handler._send_json({"connected": False, "error": "Cannot reach scanner"})
    elif protocol == "wsd":
        from ..scanner.wsd import WsdScanner
        wsd = WsdScanner()
        connected = wsd.test_connection(scanner_info)
        if connected:
            caps = wsd.get_capabilities(scanner_info)
            handler._send_json({
                "connected": True,
                "capabilities": caps.to_dict(),
            })
        else:
            handler._send_json({"connected": False, "error": "Cannot reach scanner"})
    else:
        handler._send_json({"connected": False, "error": f"Unknown protocol: {protocol}"})


@route("POST", "/api/test-scan")
def api_test_scan(handler, query):
    """Perform a test scan and return metadata (not the actual PDF)."""
    body = handler.read_json_body()

    scanner_info = ScannerInfo(
        name=body.get("name", ""),
        ip=body.get("ip", ""),
        port=int(body.get("port", 80)),
        protocol=body.get("protocol", "escl"),
    )

    source = body.get("source", "Platen")
    settings = ScanSettings(
        source=ScanSource.ADF if source == "Feeder" else ScanSource.FLATBED,
        resolution=150,  # Low res for test scan speed
    )

    protocol = body.get("protocol", "escl")
    try:
        if protocol == "escl":
            from ..scanner.escl import EsclScanner
            pdf_data = EsclScanner().scan(scanner_info, settings)
        elif protocol == "wsd":
            from ..scanner.wsd import WsdScanner
            pdf_data = WsdScanner().scan(scanner_info, settings)
        else:
            handler._send_json({"error": f"Unknown protocol: {protocol}"}, status=400)
            return

        handler._send_json({
            "success": True,
            "size_bytes": len(pdf_data),
            "size_readable": _format_size(len(pdf_data)),
        })
    except Exception as e:
        logger.exception("Test scan failed")
        handler._send_json({"success": False, "error": str(e)})


@route("POST", "/api/test-paperless")
def api_test_paperless(handler, query):
    """Test Paperless-NGX connection."""
    body = handler.read_json_body()
    url = body.get("url", "")
    token = body.get("token", "")

    if not url:
        handler._send_json({"error": "Paperless URL required"}, status=400)
        return

    result = test_paperless_connection(url, token)
    handler._send_json(result)


@route("POST", "/api/save-config")
def api_save_config(handler, query):
    """Save the wizard configuration."""
    body = handler.read_json_body()

    config = get_config()

    # Scanner
    scanner_data = body.get("scanner", {})
    config.scanner = ScannerConfig(
        ip=scanner_data.get("ip", ""),
        port=int(scanner_data.get("port", 80)),
        protocol=scanner_data.get("protocol", "escl"),
        name=scanner_data.get("name", ""),
        model=scanner_data.get("model", ""),
    )

    # Output
    output_data = body.get("output", {})
    config.output = OutputConfig(
        folder=output_data.get("folder", ""),
        filename_pattern=output_data.get("filename_pattern", "scan_{date}_{time}_{n}"),
    )

    # Paperless
    paperless_data = body.get("paperless", {})
    config.paperless = PaperlessConfig(
        enabled=paperless_data.get("enabled", False),
        mode=paperless_data.get("mode", "consume"),
        consume_folder=paperless_data.get("consume_folder", ""),
        api_url=paperless_data.get("api_url", ""),
        api_token=paperless_data.get("api_token", ""),
        default_tags=paperless_data.get("default_tags", []),
    )

    # Monitor
    monitor_data = body.get("monitor", {})
    config.monitor = MonitorConfig(
        enabled=monitor_data.get("enabled", False),
        poll_interval=float(monitor_data.get("poll_interval", 3.0)),
    )

    # Folder watch
    folder_data = body.get("folder_watch", {})
    if folder_data.get("enabled"):
        from ..config import FolderWatchConfig
        config.folder_watch = FolderWatchConfig(
            enabled=True,
            watch_folder=folder_data.get("watch_folder", ""),
        )

    # FTP receiver
    ftp_data = body.get("ftp_receive", {})
    config.ftp_receive = FtpReceiveConfig(
        enabled=ftp_data.get("enabled", False),
        port=int(ftp_data.get("port", 2121)),
        username=ftp_data.get("username", "scan"),
        password=ftp_data.get("password", "scan"),
        delete_after_routing=ftp_data.get("delete_after_routing", True),
    )

    config.wizard_completed = True
    save_config(config)
    set_config(config)

    handler._send_json({"saved": True})


@route("GET", "/api/wizard-status")
def api_wizard_status(handler, query):
    """Get current wizard completion state."""
    config = get_config()
    handler._send_json({
        "wizard_completed": config.wizard_completed,
        "has_scanner": bool(config.scanner.ip),
        "has_output": bool(config.output.folder),
    })


def _format_size(size_bytes: int) -> str:
    for unit in ("B", "KB", "MB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} GB"
