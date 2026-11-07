"""Separator sheet download: GET /api/separator-sheet?label=Invoices

Returns a one-page Letter PDF with a QR code for "OSH:<label>". Put the
printed sheet in front of each document in a feeder batch; a profile that
splits by barcode starts a new document there and names it <label>.
"""

import logging
import re

from .. import separators
from .server import BadRequest, route

logger = logging.getLogger(__name__)


@route("GET", "/api/separator-sheet")
def api_separator_sheet(handler, query):
    label = " ".join(((query.get("label") or [""])[0]).split())
    if not label:
        raise BadRequest("Give the sheet a label, for example ?label=Invoices.")
    if len(label) > separators.MAX_LABEL:
        raise BadRequest(f"The label can have at most {separators.MAX_LABEL} characters.")
    try:
        data = separators.separator_sheet(label)
    except ImportError:
        raise BadRequest("Barcode support is not installed, so no sheet can be made.", 503)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", label).strip("_") or "sheet"
    handler.send_response(200)
    handler.send_header("Content-Type", "application/pdf")
    handler.send_header("Content-Disposition", f'attachment; filename="separator_{name}.pdf"')
    handler.send_header("Content-Length", str(len(data)))
    handler.send_header("Cache-Control", "no-store")
    handler._common_headers()
    handler.end_headers()
    handler.wfile.write(data)
