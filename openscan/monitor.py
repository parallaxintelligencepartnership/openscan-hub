"""ADF/device state monitor for auto-scanning."""

import logging
import threading
import time
from typing import Optional

from .config import AppConfig
from .output import generate_filename, save_scan
from .scanner.base import (
    AdfState,
    ScannerInfo,
    ScannerState,
    ScanSettings,
    ScanSource,
)

logger = logging.getLogger(__name__)

_monitor_thread: Optional[threading.Thread] = None
_stop_event = threading.Event()


def start_monitor(config: AppConfig) -> None:
    """Start the ADF monitor daemon thread."""
    global _monitor_thread

    if _monitor_thread and _monitor_thread.is_alive():
        logger.warning("Monitor already running")
        return

    _stop_event.clear()
    _monitor_thread = threading.Thread(
        target=_monitor_loop,
        args=(config,),
        daemon=True,
        name="adf-monitor",
    )
    _monitor_thread.start()
    logger.info(
        "ADF monitor started (poll every %.1fs)",
        config.monitor.poll_interval,
    )


def stop_monitor() -> None:
    """Stop the ADF monitor thread."""
    _stop_event.set()
    if _monitor_thread:
        _monitor_thread.join(timeout=5)
    logger.info("ADF monitor stopped")


def _monitor_loop(config: AppConfig) -> None:
    """Main monitor loop - watches for ADF state transitions."""
    scanner_info = ScannerInfo(
        name=config.scanner.name,
        ip=config.scanner.ip,
        port=config.scanner.port,
        protocol=config.scanner.protocol,
    )

    protocol = config.scanner.protocol
    if protocol == "escl":
        from .scanner.escl import EsclScanner
        scanner_impl = EsclScanner()
    elif protocol == "wsd":
        from .scanner.wsd import WsdScanner
        scanner_impl = WsdScanner()
    else:
        logger.error("Monitor not supported for protocol: %s", protocol)
        return

    prev_adf_state = AdfState.UNKNOWN

    while not _stop_event.is_set():
        try:
            status = scanner_impl.get_status(scanner_info)

            # Detect paper loaded transition
            if (
                status.adf_state == AdfState.LOADED
                and prev_adf_state != AdfState.LOADED
                and status.state == ScannerState.IDLE
            ):
                logger.info("Paper detected in ADF - waiting for settle...")
                time.sleep(config.monitor.settle_time)

                # Re-check state after settle
                status = scanner_impl.get_status(scanner_info)
                if (
                    status.adf_state == AdfState.LOADED
                    and status.state == ScannerState.IDLE
                ):
                    logger.info("Auto-scanning from ADF...")
                    _auto_scan(config, scanner_info, scanner_impl)

            prev_adf_state = status.adf_state

        except Exception as e:
            logger.debug("Monitor poll error: %s", e)

        _stop_event.wait(config.monitor.poll_interval)


def _auto_scan(config: AppConfig, scanner_info: ScannerInfo, scanner_impl) -> None:
    """Execute an automatic scan triggered by ADF paper detection."""
    settings = ScanSettings(source=ScanSource.ADF)

    try:
        pdf_data = scanner_impl.scan(scanner_info, settings)

        filename = generate_filename(config.output.filename_pattern)
        results = save_scan(
            pdf_data=pdf_data,
            output_folder=config.output.folder,
            filename=filename,
            paperless_consume=(
                config.paperless.consume_folder
                if config.paperless.enabled and config.paperless.mode == "consume"
                else None
            ),
            paperless_api_url=(
                config.paperless.api_url
                if config.paperless.enabled and config.paperless.mode == "api"
                else None
            ),
            paperless_api_token=(
                config.paperless.api_token
                if config.paperless.enabled and config.paperless.mode == "api"
                else None
            ),
            paperless_tags=(
                config.paperless.default_tags
                if config.paperless.enabled
                else None
            ),
        )

        logger.info("Auto-scan complete: %s (%d bytes)", filename, len(pdf_data))

        # Add to dashboard history if available
        try:
            from .web.dashboard_api import add_to_history
            from datetime import datetime
            add_to_history({
                "filename": filename,
                "timestamp": datetime.now().isoformat(),
                "size_bytes": len(pdf_data),
                "source": "Feeder",
                "auto": True,
                "results": results,
            })
        except ImportError:
            pass

    except Exception as e:
        logger.error("Auto-scan failed: %s", e)
