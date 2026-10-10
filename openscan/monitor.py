"""ADF monitor: scans automatically when paper is put in the document feeder.

The monitor and every other scan (dashboard, multipage page, test scan)
share `scanner.base.scan_lock`. The monitor never waits for the lock: if
another scan holds it, the cycle is skipped. It also stays idle while a
multipage session is in use.
"""

import dataclasses
import logging
import threading
from datetime import datetime
from typing import Optional

from .config import AppConfig
from .output import generate_filename, save_scan
from .scanner.base import (
    AdfState,
    ScannerInfo,
    ScannerState,
    ScanSettings,
    ScanSource,
    release_scan,
    try_acquire_scan,
)

logger = logging.getLogger(__name__)

MAX_BACKOFF = 60.0  # seconds between polls while the scanner is offline
JOIN_TIMEOUT = 5.0

_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop: Optional[threading.Event] = None
_config: Optional[AppConfig] = None


def start_monitor(config: AppConfig) -> None:
    """Start (or update) the ADF monitor. Stops it when monitoring is disabled.

    Idempotent: calling it while running only swaps in the new config,
    which the loop picks up on its next poll.
    """
    global _thread, _stop, _config
    if not config.monitor.enabled:
        if monitor_running():
            logger.info("Automatic feeder scanning turned off")
        stop_monitor()
        return
    with _lock:
        _config = config
        if _thread is not None and _thread.is_alive():
            return
        _stop = threading.Event()
        _thread = threading.Thread(
            target=_monitor_loop, args=(_stop,), daemon=True, name="adf-monitor"
        )
        _thread.start()
    logger.info("ADF monitor started (checks every %gs)", config.monitor.poll_interval)


def stop_monitor() -> None:
    """Stop the monitor and wait up to 5 s. Safe if it never started."""
    global _thread
    with _lock:
        thread, stop = _thread, _stop
        if stop is not None:
            stop.set()
    if thread is None:
        return
    if thread is not threading.current_thread():
        thread.join(JOIN_TIMEOUT)
    with _lock:
        if _thread is thread:
            _thread = None
    logger.info("ADF monitor stopped")


def monitor_running() -> bool:
    with _lock:
        return _thread is not None and _thread.is_alive()


def _make_impl(protocol: str):
    """Protocol implementation for the monitor (None if unsupported)."""
    if protocol == "escl":
        from .scanner.escl import EsclScanner
        return EsclScanner()
    if protocol == "wsd":
        from .scanner.wsd import WsdScanner
        return WsdScanner()
    return None


def _multipage_active() -> bool:
    try:
        from .multipage import active_session_count
        return active_session_count() > 0
    except Exception:
        return False


def _lock_free() -> bool:
    """True if no other scan holds the scanner lock right now."""
    if try_acquire_scan(0):
        release_scan()
        return True
    return False


def _monitor_loop(stop: threading.Event) -> None:
    impl = None
    impl_key = None
    scanner_info = None
    prev_adf: Optional[AdfState] = None  # None = no baseline yet
    problem: Optional[str] = None  # last logged problem, to log once per change
    backoff = 0.0

    def report(kind: Optional[str], msg: str = "", *args) -> None:
        """Log only when the problem state changes (never once per poll)."""
        nonlocal problem
        if kind == problem:
            return
        if kind is not None:
            logger.warning(msg, *args)
        elif problem == "offline":
            logger.info("ADF monitor: scanner is responding again")
        elif problem == "jam":
            logger.info("ADF monitor: paper jam cleared")
        problem = kind

    while not stop.is_set():
        config = _config
        interval = max(0.2, float(config.monitor.poll_interval or 3.0))
        wait = interval
        sc = config.scanner
        key = (sc.protocol, sc.ip, sc.port, sc.path)
        if key != impl_key:
            impl_key = key
            impl = _make_impl(sc.protocol) if sc.ip else None
            scanner_info = ScannerInfo(name=sc.name, ip=sc.ip, port=sc.port,
                                       protocol=sc.protocol, path=sc.path)
            prev_adf = None
            backoff = 0.0

        if impl is None:
            report("unsupported", "ADF monitor: no network scanner set up (protocol %r)", sc.protocol)
        elif _multipage_active() or not _lock_free():
            pass  # another scan or a multipage session owns the scanner
        else:
            try:
                status = impl.get_status(scanner_info)
                error = None
            except Exception as e:  # a protocol bug must not kill the thread
                status, error = None, e

            if status is None or status.state == ScannerState.OFFLINE:
                report("offline",
                       "ADF monitor: cannot reach the scanner at %s%s; will keep trying",
                       sc.ip, f" ({error})" if error else "")
                backoff = min(MAX_BACKOFF, max(interval, backoff * 2 or interval * 2))
                wait = backoff
                prev_adf = None
            else:
                backoff = 0.0
                adf = status.adf_state
                if adf == AdfState.JAMMED:
                    report("jam", "ADF monitor: the document feeder reports a paper jam")
                else:
                    report(None)
                if (
                    prev_adf is not None
                    and adf == AdfState.LOADED
                    and prev_adf != AdfState.LOADED
                    and status.state == ScannerState.IDLE
                ):
                    logger.info("Paper detected in the document feeder")
                    if stop.wait(max(0.0, float(config.monitor.settle_time))):
                        break
                    _try_auto_scan(config, scanner_info, impl)
                prev_adf = adf

        stop.wait(wait)


def _try_auto_scan(config: AppConfig, scanner_info: ScannerInfo, impl) -> bool:
    """Scan from the feeder if nothing else is using the scanner."""
    if _multipage_active():
        logger.info("Auto-scan skipped: a multi-page scan is in progress")
        return False
    if not try_acquire_scan(0):
        logger.info("Auto-scan skipped: another scan is in progress")
        return False
    try:
        status = impl.get_status(scanner_info)
        if status.adf_state != AdfState.LOADED or status.state != ScannerState.IDLE:
            return False
        logger.info("Auto-scanning from the document feeder")
        _auto_scan(config, scanner_info, impl)
        return True
    finally:
        release_scan()


def _delivery_ok(result) -> bool:
    """Success flag from save_scan: DeliveryResult (.ok) or the old dict."""
    if hasattr(result, "ok"):
        return bool(result.ok)
    if isinstance(result, dict):
        return bool(result.get("saved")) and not result.get("errors")
    return bool(result)


def _as_dict(result):
    if dataclasses.is_dataclass(result) and not isinstance(result, type):
        return dataclasses.asdict(result)
    return result


def _auto_scan(config: AppConfig, scanner_info: ScannerInfo, scanner_impl) -> None:
    """Execute an automatic feeder scan and deliver it. Caller holds scan_lock."""
    d = config.scan
    settings = ScanSettings(
        source=ScanSource.ADF,
        resolution=d.resolution,
        color_mode=d.color_mode,
        duplex=d.duplex,
        page_size=d.page_size,
        remove_blank_pages=d.remove_blank_pages,
    )
    try:
        pdf_data = scanner_impl.scan(scanner_info, settings)
    except Exception as e:
        logger.error("Auto-scan failed: %s", e)
        return

    p = config.paperless
    filename = generate_filename(config.output.filename_pattern)
    try:
        result = save_scan(
            pdf_data=pdf_data,
            output_folder=config.output.folder,
            filename=filename,
            paperless_consume=p.consume_folder if p.enabled and p.mode == "consume" else None,
            paperless_api_url=p.api_url if p.enabled and p.mode == "api" else None,
            paperless_api_token=p.api_token if p.enabled and p.mode == "api" else None,
            paperless_tags=p.tags if p.enabled else None,
        )
    except Exception as e:
        logger.error("Auto-scan %s could not be saved: %s", filename, e)
        return

    if _delivery_ok(result):
        logger.info("Auto-scan saved: %s (%d bytes)", filename, len(pdf_data))
    else:
        logger.error("Auto-scan %s was not delivered everywhere: %s", filename,
                     getattr(result, "errors", None) or (result.get("errors") if isinstance(result, dict) else result))

    try:
        from .web.dashboard_api import add_to_history
        add_to_history({
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
            "size_bytes": len(pdf_data),
            "source": "Feeder",
            "auto": True,
            "results": _as_dict(result),
        })
    except Exception as e:
        logger.debug("Could not add auto-scan to history: %s", e)
