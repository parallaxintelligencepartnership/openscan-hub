"""Optional Home Assistant module.

Routes exist only while ``config.homeassistant.enabled`` is on (otherwise 404).
They need the access token in the ``X-OpenScan-Token`` header (or
``Authorization: Bearer``) or as ``?token=``, so Home Assistant's
``rest_command`` can call them. A browser session cookie is not enough.

- ``GET|POST /api/ha/scan?source=adf|flatbed`` starts a scan and returns at
  once (202); 409 when the scanner is busy.
- ``GET /api/ha/health`` reports whether the scanner is reachable.
"""

import logging

from ..config import AppConfig

logger = logging.getLogger(__name__)

_registered = False


def enabled(config: AppConfig) -> bool:
    return bool(config.homeassistant.enabled)


def _scan(handler, query):
    from ..web.dashboard_api import BUSY_MESSAGE, parse_source, start_scan_async

    source = parse_source((query.get("source") or ["flatbed"])[0])
    if handler.command == "POST" and not query.get("source"):
        from ..web.server import BadRequest
        try:
            body = handler.read_json_body()
        except BadRequest:
            body = {}
        source = parse_source(body.get("source", "flatbed"))
    if start_scan_async(source):
        handler._send_json({"started": True, "source": "adf" if source.value == "Feeder" else "flatbed"},
                           status=202)
    else:
        handler._send_json({"started": False, "error": BUSY_MESSAGE}, status=409)


def _health(handler, query):
    from .. import __version__
    from ..web.dashboard_api import queue_pending, scanner_busy, scanner_online
    from ..web.server import get_config

    handler._send_json({
        "status": "ok",
        "version": __version__,
        "scanner_online": scanner_online(get_config()),
        "scan_in_progress": scanner_busy(),
        "queue_pending": len(queue_pending()),
    })


def register() -> None:
    """Register the routes once; each request checks whether the module is on."""
    global _registered
    if _registered:
        return
    from ..web.server import route

    opts = dict(query_token=True, cookie=False, json_only=False, guard=enabled)
    route("GET", "/api/ha/scan", **opts)(_scan)
    route("POST", "/api/ha/scan", **opts)(_scan)
    route("GET", "/api/ha/health", **opts)(_health)
    _registered = True
