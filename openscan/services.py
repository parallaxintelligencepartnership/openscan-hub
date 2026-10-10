"""Start, stop and restart background services when settings change (review I4).

Each service is described by the config sections it reads. When any of those
sections change, a running service is restarted with the new config; when its
"wanted" state flips it is started or stopped. Services are imported lazily so
a missing optional module never stops the app.
"""

import importlib
import logging
import threading
from dataclasses import asdict, dataclass
from typing import Callable, Optional

from .config import AppConfig, get_config_dir

logger = logging.getLogger(__name__)

_lock = threading.RLock()
_running_web: Optional[tuple[str, int]] = None  # (bind, port) the server actually uses


def _is_loopback(bind: str) -> bool:
    return bind in ("127.0.0.1", "::1", "localhost") or bind.startswith("127.")


def _driven_scanner(c: AppConfig) -> bool:
    return c.scanner.protocol in ("escl", "wsd") and bool(c.scanner.ip)


@dataclass
class Service:
    name: str
    module: str
    start: str
    stop: str
    wanted: Callable[[AppConfig], bool]
    sections: tuple[str, ...]
    start_with: str = "config"  # "config" passes the AppConfig, "mdns" passes bind/port


DELIVERY = ("output", "paperless", "scan")

SERVICES: list[Service] = [
    Service(
        "ADF monitor", "openscan.monitor", "start_monitor", "stop_monitor",
        lambda c: c.wizard_completed and c.monitor.enabled and _driven_scanner(c),
        ("scanner", "monitor") + DELIVERY,
    ),
    Service(
        "folder watcher", "openscan.scanner.folder_watch", "start_folder_watcher", "stop_folder_watcher",
        lambda c: c.wizard_completed and c.folder_watch.enabled and bool(c.folder_watch.watch_folder),
        ("folder_watch",) + DELIVERY,
    ),
    Service(
        "FTP receiver", "openscan.scanner.ftp_receive", "start_ftp_receiver", "stop_ftp_receiver",
        lambda c: c.wizard_completed and c.ftp_receive.enabled,
        ("ftp_receive",) + DELIVERY,
    ),
    Service(
        "retry queue", "openscan.retry_queue", "start", "stop",
        lambda c: True,
        ("output", "paperless"),
    ),
    Service(
        "network name (mDNS)", "openscan.mdns", "start", "stop",
        lambda c: _mdns_wanted(c),
        ("web",),
        start_with="mdns",
    ),
]


def _mdns_wanted(c: AppConfig) -> bool:
    if not c.web.advertise_mdns:
        return False
    bind = _running_web[0] if _running_web else c.web.bind
    return not _is_loopback(bind)


def _load(modpath: str):
    """Import a service module. Tests replace this."""
    return importlib.import_module(modpath)


def _target(svc: Service, fn_name: str) -> Optional[Callable]:
    try:
        mod = _load(svc.module)
    except ImportError as e:
        logger.debug("%s not available: %s", svc.name, e)
        return None
    if svc.module == "openscan.retry_queue":
        get_queue = getattr(mod, "get_queue", None)
        if get_queue is None:
            return None
        return getattr(get_queue(), fn_name, None)
    return getattr(mod, fn_name, None)


def _call(svc: Service, fn_name: str, config: Optional[AppConfig], errors: list[str]) -> bool:
    func = _target(svc, fn_name)
    if func is None:
        return False
    try:
        if config is None:
            func()
        elif svc.start_with == "mdns":
            bind, port = _running_web or (config.web.bind, config.web.port)
            func(port=port, bind=bind)
        else:
            func(config)
        return True
    except Exception as e:
        logger.error("Could not %s the %s: %s", "start" if config else "stop", svc.name, e)
        errors.append(f"Could not {'start' if config else 'stop'} the {svc.name}: {e}")
        return False


def _prepare(config: AppConfig) -> None:
    if config.ftp_receive.enabled and not config.ftp_receive.staging_dir:
        config.ftp_receive.staging_dir = str(get_config_dir() / "ftp_staging")


def _section_changed(old: AppConfig, new: AppConfig, sections: tuple[str, ...]) -> bool:
    return any(asdict(getattr(old, s)) != asdict(getattr(new, s)) for s in sections)


def set_running_web(bind: str, port: int) -> None:
    """Record what the web server actually bound to (bind/port changes need a restart)."""
    global _running_web
    _running_web = (bind, port)


def restart_needed(config: AppConfig) -> bool:
    if _running_web is None:
        return False
    return (config.web.bind, config.web.port) != _running_web


def apply_config(old: Optional[AppConfig], new: AppConfig) -> dict:
    """Bring the running services in line with `new`.

    `old` is the config the services were started with (None at startup).
    Returns {"started": [...], "stopped": [...], "restarted": [...],
    "errors": [...], "restart_needed": bool}.
    """
    report = {"started": [], "stopped": [], "restarted": [], "errors": [], "restart_needed": False}
    with _lock:
        _prepare(new)
        for svc in SERVICES:
            want_new = bool(svc.wanted(new))
            want_old = bool(svc.wanted(old)) if old is not None else False
            if old is not None and want_old and not want_new:
                if _call(svc, svc.stop, None, report["errors"]):
                    report["stopped"].append(svc.name)
            elif want_new and not want_old:
                if _call(svc, svc.start, new, report["errors"]):
                    report["started"].append(svc.name)
            elif want_new and want_old and _section_changed(old, new, svc.sections):
                _call(svc, svc.stop, None, report["errors"])
                if _call(svc, svc.start, new, report["errors"]):
                    report["restarted"].append(svc.name)
        report["restart_needed"] = restart_needed(new)
    for key in ("started", "stopped", "restarted"):
        if report[key]:
            logger.info("Services %s: %s", key, ", ".join(report[key]))
    return report


def start_all(config: AppConfig) -> dict:
    return apply_config(None, config)


def stop_all() -> None:
    errors: list[str] = []
    with _lock:
        for svc in reversed(SERVICES):
            _call(svc, svc.stop, None, errors)
