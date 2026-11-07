"""Dashboard API: status, scanning, history, multi-page sessions, health."""

import logging
import threading
import time
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Optional

from .. import __version__
from .. import multipage
from ..config import AppConfig, config_dir_writable
from .. import intake, profiles
from ..profiles import Profile
from ..scanner.base import (
    ScannerInfo, ScanSettings, ScanSource, release_scan, scan_lock, try_acquire_scan,
)
from .server import BadRequest, route, get_config

logger = logging.getLogger(__name__)

BUSY_MESSAGE = "Scanner is busy"
NO_DRIVE_MESSAGE = ("This setup does not start scans from here. Your scanner sends files "
                    "to OpenScanHub by itself.")

_START_TIME = time.monotonic()

_scan_history: list[dict] = []
_history_lock = threading.Lock()
MAX_HISTORY = 50

# Scanner capabilities are cached so the 5 second status poll stays cheap.
_CAPS_TTL_OK = 600.0
_CAPS_TTL_FAIL = 60.0
_caps_cache: dict[tuple, tuple[float, list[str]]] = {}
_caps_lock = threading.Lock()

_ONLINE_TTL = 15.0
_online_cache: dict[str, Any] = {"time": 0.0, "value": None, "key": None}


class ScannerBusy(Exception):
    pass


# --- helpers -------------------------------------------------------------

def add_to_history(entry: dict) -> None:
    """Record a scan. Entries carrying only "results" (a DeliveryResult dict)
    get the flat ok/queued/saved_paths/errors keys the dashboard reads."""
    results = entry.get("results")
    if "ok" not in entry and results is not None:
        entry = {**entry, **delivery_summary(results)}
    with _history_lock:
        _scan_history.insert(0, entry)
        del _scan_history[MAX_HISTORY:]


def parse_source(value: Any) -> ScanSource:
    """"adf"/"feeder"/"Feeder" mean the document feeder; anything else is the flatbed."""
    if isinstance(value, str) and value.strip().lower() in ("adf", "feeder"):
        return ScanSource.ADF
    return ScanSource.FLATBED


def scanner_mode(config: AppConfig) -> str:
    """"scanner" when we drive a scanner, "ftp"/"folder" when files arrive by themselves."""
    if config.scanner.protocol in ("escl", "wsd") and config.scanner.ip:
        return "scanner"
    if config.ftp_receive.enabled or config.scanner.protocol == "ftp":
        return "ftp"
    if config.folder_watch.enabled or config.scanner.protocol == "folder":
        return "folder"
    return "none"


def scanner_busy() -> bool:
    """True while any thread (dashboard, monitor, multipage, tray) holds the scan lock."""
    if scan_lock.acquire(blocking=False):
        scan_lock.release()
        return False
    return True


def delivery_summary(result: Any) -> dict:
    """Normalize what save_scan returns.

    New shape: DeliveryResult (ok, saved_paths, errors, queued). Old shape: a dict
    with "saved" and "errors" lists. Either way the answer is the same dict.
    """
    if isinstance(result, dict) and "ok" in result:  # DeliveryResult.to_dict()
        result = SimpleNamespace(**result)
    if hasattr(result, "ok"):
        return {
            "ok": bool(result.ok),
            "queued": bool(getattr(result, "queued", False)),
            "saved_paths": [str(p) for p in (getattr(result, "saved_paths", None) or [])],
            "errors": [str(e) for e in (getattr(result, "errors", None) or [])],
        }
    if isinstance(result, dict):
        saved = result.get("saved") or []
        errors = result.get("errors") or []
        paths = [str(s.get("path")) for s in saved if isinstance(s, dict) and s.get("path")]
        msgs = []
        for e in errors:
            if isinstance(e, dict):
                msgs.append(f"{e.get('type', 'destination')}: {e.get('error', 'failed')}")
            else:
                msgs.append(str(e))
        return {"ok": bool(saved) and not errors, "queued": False,
                "saved_paths": paths, "errors": msgs}
    return {"ok": bool(result), "queued": False, "saved_paths": [], "errors": []}


def _scanner_info(config: AppConfig) -> ScannerInfo:
    return ScannerInfo(
        name=config.scanner.name,
        ip=config.scanner.ip,
        port=config.scanner.port,
        protocol=config.scanner.protocol,
        model=config.scanner.model,
        path=config.scanner.path,
    )


def _impl(protocol: str):
    if protocol == "escl":
        from ..scanner.escl import EsclScanner
        return EsclScanner()
    if protocol == "wsd":
        from ..scanner.wsd import WsdScanner
        return WsdScanner()
    raise ValueError(f"Unknown protocol: {protocol}")


def scanner_sources(config: AppConfig) -> list[str]:
    """Sources the scanner reports ("Platen", "Feeder"), cached."""
    key = (config.scanner.protocol, config.scanner.ip, config.scanner.port, config.scanner.path)
    now = time.monotonic()
    with _caps_lock:
        hit = _caps_cache.get(key)
        if hit and hit[0] > now:
            return list(hit[1])
    try:
        caps = _impl(config.scanner.protocol).get_capabilities(_scanner_info(config))
        sources = [s.value for s in caps.sources]
        ttl = _CAPS_TTL_OK
    except Exception as e:
        logger.debug("Could not read scanner capabilities: %s", e)
        sources, ttl = [], _CAPS_TTL_FAIL
    with _caps_lock:
        _caps_cache[key] = (now + ttl, sources)
    return list(sources)


def clear_caps_cache() -> None:
    with _caps_lock:
        _caps_cache.clear()


def _service_value(modpath: str, fn: str):
    """Value of a plain status function of a service module (None if absent)."""
    import importlib
    try:
        func = getattr(importlib.import_module(modpath), fn, None)
        return func() if func else None
    except Exception:
        return None


def _service_running(modpath: str, fn: str) -> Optional[bool]:
    import importlib
    try:
        mod = importlib.import_module(modpath)
    except ImportError:
        return None
    func = getattr(mod, fn, None)
    if func is None:
        return None
    try:
        return bool(func())
    except Exception:
        return None


def _remember_online(config: AppConfig, value: Optional[bool]) -> None:
    _online_cache.update(time=time.monotonic(), value=value,
                         key=(config.scanner.protocol, config.scanner.ip))


def scanner_online(config: AppConfig) -> Optional[bool]:
    """Cached online state for /api/health. None when nothing is configured."""
    mode = scanner_mode(config)
    if mode == "ftp":
        return _service_running("openscan.scanner.ftp_receive", "ftp_receiver_running")
    if mode == "folder":
        return _service_running("openscan.scanner.folder_watch", "folder_watcher_running")
    if mode != "scanner":
        return None
    key = (config.scanner.protocol, config.scanner.ip)
    if _online_cache["key"] == key and time.monotonic() - _online_cache["time"] < _ONLINE_TTL:
        return _online_cache["value"]
    try:
        status = _impl(config.scanner.protocol).get_status(_scanner_info(config))
        value = status.state.value != "Offline"
    except Exception:
        value = False
    _remember_online(config, value)
    return value


def queue_pending() -> list[dict]:
    try:
        from .. import retry_queue  # lane C module; may be absent before merge
    except ImportError:
        return []
    try:
        return list(retry_queue.get_queue().pending())
    except Exception as e:
        logger.debug("Retry queue unavailable: %s", e)
        return []


def _scan_settings(config: AppConfig, source: ScanSource, resolution: Optional[int] = None,
                   remove_blank_pages: Optional[bool] = None,
                   profile: Optional[Profile] = None) -> ScanSettings:
    """Scanner settings from `profile` (default: the default profile). The
    migrated Default profile follows the Settings page, as before."""
    profile = profiles.get(config) if profile is None else profiles.live(config, profile)
    return profiles.scan_settings(profile, source, resolution=resolution,
                                  remove_blank_pages=remove_blank_pages)


def pick_source(config: AppConfig, profile: Profile, requested: Any = None) -> ScanSource:
    """Where to scan from: the profile's source, else `requested`, else the
    feeder when it reports paper, else the flatbed."""
    if profile.source == "adf":
        return ScanSource.ADF
    if profile.source == "flatbed":
        return ScanSource.FLATBED
    if isinstance(requested, str) and requested.strip():
        return parse_source(requested)
    try:
        status = _impl(config.scanner.protocol).get_status(_scanner_info(config))
        if status.adf_state.value == "ScannerAdfLoaded":
            return ScanSource.ADF
    except Exception as e:
        logger.debug("Could not read the feeder state: %s", e)
    return ScanSource.FLATBED


def execute_scan(config: AppConfig, source: ScanSource, resolution: Optional[int] = None,
                 remove_blank_pages: Optional[bool] = None,
                 profile: Optional[Profile] = None) -> bytes:
    """Drive the scanner and return the raw document. Caller must hold the scan lock."""
    settings = _scan_settings(config, source, resolution, remove_blank_pages, profile)
    return _impl(config.scanner.protocol).scan(_scanner_info(config), settings)


def save_document(config: AppConfig, pdf_data: bytes, source: str = "flatbed",
                  started_by: str = "web", profile: Optional[Profile] = None) -> dict:
    """Process, name and deliver through intake.handle_scan. Returns
    delivery_summary() plus "filename" (the name actually written),
    "size_bytes", "page_count" and "warnings"."""
    result = intake.handle_scan(config, pdf_data, source=source, started_by=started_by,
                                profile=profile)
    summary = delivery_summary(result)
    summary["filename"] = getattr(result, "filename", "") or "scan.pdf"
    summary["size_bytes"] = getattr(result, "size_bytes", 0) or len(pdf_data)
    summary["page_count"] = getattr(result, "page_count", 0)
    summary["warnings"] = list(getattr(result, "warnings", []) or [])
    return summary


def _source_id(source: ScanSource) -> str:
    return "adf" if source == ScanSource.ADF else "flatbed"


def _history_entry(summary: dict, size: int, source: str, **extra) -> dict:
    results = {k: summary[k] for k in ("ok", "saved_paths", "errors", "queued")}
    return {
        "filename": summary["filename"],
        "timestamp": datetime.now().isoformat(),
        "size_bytes": size,
        "source": source,
        **extra,
        **results,
        "results": results,
    }


def _page_count(pdf_data: bytes, fallback: int) -> int:
    try:
        from pypdf import PdfReader
        from io import BytesIO
        return len(PdfReader(BytesIO(pdf_data)).pages)
    except Exception:
        return fallback


def scan_message(summary: dict, filename: str) -> str:
    if summary["ok"]:
        return f"Saved {filename}"
    if summary["queued"]:
        return (f"Scanned {filename}, but it could not be delivered yet. "
                "It will be retried automatically.")
    detail = "; ".join(summary["errors"]) or "no destination accepted the file"
    return f"Scanned {filename}, but saving failed: {detail}"


def web_started_by(handler) -> str:
    """Who started a scan from the web: the role and the address."""
    role = getattr(handler, "request_role", None) or "admin"
    return f"web {role} {handler.peer_ip()}"


def run_scan(source: ScanSource, config: Optional[AppConfig] = None,
             started_by: str = "web", profile: Optional[Profile] = None) -> dict:
    """Scan and deliver one document with `profile` (default: the default
    profile). Raises ScannerBusy if another scan holds the lock."""
    config = config or get_config()
    if scanner_mode(config) != "scanner":
        raise BadRequest(NO_DRIVE_MESSAGE)
    profile = profiles.get(config) if profile is None else profiles.live(config, profile)
    if not try_acquire_scan(timeout=0):
        raise ScannerBusy(BUSY_MESSAGE)
    try:
        raw = execute_scan(config, source, profile=profile)
        pdf_data = multipage.ensure_pdf(raw, resolution=profile.resolution)
    finally:
        release_scan()
    summary = save_document(config, pdf_data, _source_id(source), started_by, profile)
    entry = _history_entry(summary, summary["size_bytes"], source.value,
                           profile=profile.name)
    add_to_history(entry)
    return {
        "success": summary["ok"],
        "message": scan_message(summary, summary["filename"]),
        **summary,
        "results": entry["results"],
    }


def start_scan_async(source: ScanSource, wait: float = 5.0, started_by: str = "web",
                     profile: Optional[Profile] = None) -> bool:
    """Start a scan on a worker thread. Returns False if the scanner is busy.

    The worker takes the lock itself (an RLock can only be released by its owner)
    and reports back whether it got it.
    """
    config = get_config()
    if scanner_mode(config) != "scanner":
        raise BadRequest(NO_DRIVE_MESSAGE)
    profile = profiles.get(config) if profile is None else profiles.live(config, profile)
    started = threading.Event()
    outcome = {"busy": True}

    def worker():
        if not try_acquire_scan(timeout=0):
            started.set()
            return
        outcome["busy"] = False
        started.set()
        try:
            try:
                raw = execute_scan(config, source, profile=profile)
                pdf_data = multipage.ensure_pdf(raw, resolution=profile.resolution)
            finally:
                release_scan()
            summary = save_document(config, pdf_data, _source_id(source), started_by, profile)
            add_to_history(_history_entry(summary, summary["size_bytes"], source.value,
                                          profile=profile.name))
            if not summary["ok"]:
                logger.warning(scan_message(summary, summary["filename"]))
        except Exception as e:
            logger.error("Scan failed: %s", e)

    threading.Thread(target=worker, daemon=True, name="scan-job").start()
    started.wait(wait)
    return not outcome["busy"]


# --- routes ----------------------------------------------------------------

@route("GET", "/api/edition")
def api_edition(handler, query):
    """Which edition this is and which optional features it has. The pages
    use it to hide controls for modules that are not in the build."""
    from .. import edition
    handler._send_json({"edition": edition.current(), "features": edition.features(),
                        "role": getattr(handler, "request_role", None) or "admin"})


@route("GET", "/api/health", public=True, query_token=True)
def api_health(handler, query):
    config = get_config()
    writable = config_dir_writable()
    handler._send_json({
        "status": "ok" if writable else "degraded",
        "config_writable": writable,
        "app": "openscanhub",
        "version": __version__,
        "scanner_online": scanner_online(config),
        "queue_pending": len(queue_pending()),
        "uptime_seconds": int(time.monotonic() - _START_TIME),
    })


@route("GET", "/api/status")
def api_status(handler, query):
    config = get_config()
    mode = scanner_mode(config)
    data: dict[str, Any] = {
        "configured": mode != "none",
        "mode": mode,
        "scan_in_progress": scanner_busy(),
        "sources": [],
        "monitor_enabled": config.monitor.enabled,
        "receivers": {
            "ftp": {
                "enabled": config.ftp_receive.enabled,
                "running": _service_running("openscan.scanner.ftp_receive", "ftp_receiver_running"),
                "port": config.ftp_receive.port,
                "username": config.ftp_receive.username,
            },
            "folder": {
                "enabled": config.folder_watch.enabled,
                "running": _service_running("openscan.scanner.folder_watch", "folder_watcher_running"),
                "folder": config.folder_watch.watch_folder,
            },
            "smb": {
                "enabled": config.smb_poll.enabled,
                "running": _service_running("openscan.scanner.smb_poll", "smb_poll_running"),
                "server": config.smb_poll.server,
                "share": config.smb_poll.share,
                "path": config.smb_poll.path,
                "error": _service_value("openscan.scanner.smb_poll", "smb_poll_last_error"),
            },
            "email": {
                "enabled": config.imap_receive.enabled,
                "running": _service_running("openscan.scanner.imap_receive", "imap_receive_running"),
                "host": config.imap_receive.host,
                "folder": config.imap_receive.folder,
                "error": _service_value("openscan.scanner.imap_receive", "imap_receive_last_error"),
            },
        },
    }
    if mode != "scanner":
        handler._send_json(data)
        return

    info = _scanner_info(config)
    try:
        status = _impl(config.scanner.protocol).get_status(info)
        data["status"] = status.to_dict()
        online = status.state.value != "Offline"
    except Exception as e:
        data["status"] = {"state": "Offline", "adf_state": "Unknown"}
        data["error"] = str(e)
        online = False
    _remember_online(config, online)
    data["sources"] = scanner_sources(config) if online else []
    scanner = info.to_dict()
    scanner["sources"] = data["sources"]
    data["scanner"] = scanner
    handler._send_json(data)


def send_scan(handler, source: ScanSource, profile: Optional[Profile] = None) -> None:
    """Run one scan and answer: 200 with the result, 409 busy, 500 failed."""
    try:
        result = run_scan(source, started_by=web_started_by(handler), profile=profile)
    except ScannerBusy:
        handler._send_json({"success": False, "error": BUSY_MESSAGE}, status=409)
        return
    except BadRequest:
        raise
    except Exception as e:
        logger.exception("Scan failed")
        handler._send_json({"success": False, "error": str(e)}, status=500)
        return
    handler._send_json(result)


@route("POST", "/api/scan")
def api_scan(handler, query):
    """Scan one document. Body: {"source": "adf" | "flatbed"}."""
    body = handler.read_json_body()
    send_scan(handler, parse_source(body.get("source", "flatbed")))


@route("GET", "/api/history")
def api_history(handler, query):
    try:
        limit = max(1, min(MAX_HISTORY, int(query.get("limit", [20])[0])))
    except ValueError:
        limit = 20
    with _history_lock:
        handler._send_json({"scans": _scan_history[:limit], "total": len(_scan_history)})


# --- multi-page ---------------------------------------------------------------

def _profile_for(config: AppConfig, profile_id: Any) -> Optional[Profile]:
    """The live profile with this id, or None for no id. 404 if it does not exist."""
    if profile_id in (None, ""):
        return None
    found = profiles.find(config, profile_id) if isinstance(profile_id, str) else None
    if found is None and isinstance(profile_id, str) and not config.profiles:
        implicit = profiles.from_settings(config)  # the migrated Default profile
        found = implicit if implicit.id == profile_id else None
    if found is None:
        raise BadRequest("Profile not found", status=404)
    return profiles.live(config, found)


@route("POST", "/api/multipage/start")
def api_multipage_start(handler, query):
    body = handler.read_json_body()
    config = get_config()
    if scanner_mode(config) != "scanner":
        raise BadRequest(NO_DRIVE_MESSAGE)
    profile = _profile_for(config, body.get("profile_id"))
    if profile is None:
        source = parse_source(body.get("source", "flatbed"))
    else:
        source = pick_source(config, profile, body.get("source"))
    session_id = multipage.create_session(source, profile.id if profile else "")
    handler._send_json({"session_id": session_id, "profile_id": profile.id if profile else ""})


def _session_from(body: dict):
    session_id = body.get("session_id")
    if not session_id or not isinstance(session_id, str):
        raise BadRequest("Missing session_id")
    session = multipage.get_session(session_id)
    if not session:
        raise BadRequest("Session not found or expired", status=404)
    return session_id, session


@route("POST", "/api/multipage/scan")
def api_multipage_scan(handler, query):
    body = handler.read_json_body()
    session_id, session = _session_from(body)
    config = get_config()
    if scanner_mode(config) != "scanner":
        raise BadRequest(NO_DRIVE_MESSAGE)
    profile = _profile_for(config, session.profile_id) if session.profile_id else None
    try:
        # One page per scan; blank pages are dropped when the session is saved.
        page_count = multipage.scan_page(
            session_id, lambda: execute_scan(config, session.source, remove_blank_pages=False,
                                             profile=profile))
    except multipage.ScannerBusyError:
        handler._send_json({"success": False, "error": BUSY_MESSAGE}, status=409)
        return
    except ValueError:
        raise BadRequest("Session not found or expired", status=404)
    except Exception as e:
        logger.exception("Multipage scan failed")
        handler._send_json({"success": False, "error": str(e)}, status=500)
        return
    handler._send_json({"success": True, "page_count": page_count, "page_index": page_count - 1})


@route("GET", "/api/multipage/thumbnail/")
def api_multipage_thumbnail(handler, query, path_suffix=""):
    parts = path_suffix.strip("/").split("/")
    if len(parts) != 2:
        raise BadRequest("Invalid path")
    session_id = parts[0]
    try:
        page_index = int(parts[1])
    except ValueError:
        raise BadRequest("Invalid page index")
    try:
        jpeg_bytes = multipage.get_page_thumbnail(session_id, page_index)
    except (ValueError, IndexError) as e:
        handler._send_json({"error": str(e)}, status=404)
        return
    handler.send_response(200)
    handler.send_header("Content-Type", "image/jpeg")
    handler.send_header("Content-Length", str(len(jpeg_bytes)))
    handler.send_header("Cache-Control", "private, max-age=300")
    handler._common_headers()
    handler.end_headers()
    handler.wfile.write(jpeg_bytes)


@route("POST", "/api/multipage/save")
def api_multipage_save(handler, query):
    body = handler.read_json_body()
    session_id, session = _session_from(body)
    if not session.pages:
        raise BadRequest("No pages to save")
    config = get_config()
    profile = _profile_for(config, body.get("profile_id") or session.profile_id)
    # Blank pages are removed once, by the profile's processing in intake.
    pdf_data = multipage.merge_pages(session_id)
    summary = save_document(config, pdf_data, _source_id(session.source),
                            web_started_by(handler), profile)
    page_count = summary["page_count"] or _page_count(pdf_data, len(session.pages))
    extra = {"profile": profile.name} if profile else {}
    entry = _history_entry(summary, summary["size_bytes"], session.source.value,
                           page_count=page_count, **extra)
    add_to_history(entry)
    # The pages are in the saved, queued or failed file now; on a hard failure
    # keep the session so the user can try again.
    if summary["ok"] or summary["queued"]:
        multipage.delete_session(session_id)
    handler._send_json({
        "success": summary["ok"],
        "message": scan_message(summary, summary["filename"]),
        **summary,
        "page_count": page_count,
        "results": entry["results"],
    })


@route("POST", "/api/multipage/cancel")
def api_multipage_cancel(handler, query):
    body = handler.read_json_body()
    session_id = body.get("session_id")
    if not session_id:
        raise BadRequest("Missing session_id")
    multipage.delete_session(session_id)
    handler._send_json({"success": True})


@route("GET", "/api/multipage/status/")
def api_multipage_status(handler, query, path_suffix=""):
    session_id = path_suffix.strip("/")
    if not session_id:
        raise BadRequest("Missing session_id")
    info = multipage.get_session_info(session_id)
    if not info:
        handler._send_json({"error": "Session not found or expired"}, status=404)
        return
    handler._send_json(info)
