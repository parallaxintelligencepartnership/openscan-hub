"""Audit log endpoints: the last entries as JSON and the file as a download.

The on/off switch is `audit.enabled` in the normal settings save.
"""

import logging

from .. import audit
from .server import get_config, route

logger = logging.getLogger(__name__)

MAX_ENTRIES = 200


@route("GET", "/api/audit")
def api_audit(handler, query):
    handler._send_json({
        "enabled": audit.enabled(get_config()),
        "entries": audit.recent(MAX_ENTRIES),
    })


@route("GET", "/api/audit/download")
def api_audit_download(handler, query):
    try:
        data = audit.audit_path().read_bytes()
    except FileNotFoundError:
        data = b""
    except OSError as e:
        logger.warning("Could not read the audit log: %s", e)
        data = b""
    handler.send_response(200)
    handler.send_header("Content-Type", "application/x-ndjson")
    handler.send_header("Content-Disposition", f'attachment; filename="{audit.FILENAME}"')
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler._common_headers()
    handler.end_headers()
    handler.wfile.write(data)
