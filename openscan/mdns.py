"""Advertise the web UI on the local network as "OpenScanHub" (_http._tcp)."""

import logging
import socket
import threading
from typing import Optional

logger = logging.getLogger(__name__)

SERVICE_TYPE = "_http._tcp.local."
SERVICE_NAME = "OpenScanHub"
HOST_NAME = "openscanhub.local."

_lock = threading.Lock()
_zc = None
_info = None


def _local_ipv4(bind: str) -> list[str]:
    if bind and bind not in ("0.0.0.0", "::", ""):
        return [bind]
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # picks the outgoing interface; sends nothing
            return [s.getsockname()[0]]
    except OSError:
        return []


def start(port: int, bind: str = "0.0.0.0") -> bool:
    """Register the service. Returns True when registered. Safe to call twice."""
    global _zc, _info
    with _lock:
        if _zc is not None:
            return True
        addresses = [a for a in _local_ipv4(bind) if not a.startswith("127.")]
        if not addresses:
            logger.warning("mDNS: no network address found, not advertising")
            return False
        try:
            from zeroconf import ServiceInfo, Zeroconf
        except ImportError:
            logger.warning("mDNS: zeroconf is not installed")
            return False
        info = ServiceInfo(
            SERVICE_TYPE,
            f"{SERVICE_NAME}.{SERVICE_TYPE}",
            addresses=[socket.inet_aton(a) for a in addresses],
            port=int(port),
            properties={"path": "/"},
            server=HOST_NAME,
        )
        zc: Optional[object] = None
        try:
            zc = Zeroconf()
            zc.register_service(info, allow_name_change=True)
        except Exception as e:
            logger.warning("mDNS: could not advertise OpenScanHub: %s", e)
            if zc is not None:
                try:
                    zc.close()
                except Exception:
                    pass
            return False
        _zc, _info = zc, info
        logger.info("mDNS: advertising http://%s:%d", HOST_NAME.rstrip("."), port)
        return True


def stop() -> None:
    """Unregister and close. Safe to call when not started."""
    global _zc, _info
    with _lock:
        zc, info = _zc, _info
        _zc, _info = None, None
    if zc is None:
        return
    try:
        if info is not None:
            zc.unregister_service(info)
    except Exception as e:
        logger.debug("mDNS: unregister failed: %s", e)
    try:
        zc.close()
    except Exception:
        pass
    logger.info("mDNS: stopped advertising")


def running() -> bool:
    return _zc is not None
