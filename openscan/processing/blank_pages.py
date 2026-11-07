"""Stage: remove blank pages (scanner duplex backs, separator sheets)."""

from __future__ import annotations

from .. import pages
from .options import ProcessOptions, ProcessResult
from .registry import register_stage

ORDER = 30  # after rotate/deskew/crop (10-29), before color, OCR and compression


def remove_blank_pages(pdf: bytes, options: ProcessOptions, result: ProcessResult) -> bytes:
    out, removed = pages.drop_blank_pages_bytes(pdf)
    result.pages_removed += removed
    return out


register_stage("remove_blank_pages", ORDER, remove_blank_pages,
               lambda o: bool(o.remove_blank_pages))
