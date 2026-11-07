"""Separator sheets: split one feeder batch into several documents.

    blank    a blank page ends a document and is dropped
    barcode  a page with a barcode or QR code ends a document and is dropped;
             its value is the {barcode} name field of the next document

Import this module once (intake does) to register both splitters and the
{barcode} name field.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from io import BytesIO
from typing import Callable, Optional

from pypdf import PdfReader, PdfWriter

from . import pages
from .intake import register_splitter
from .naming import register_field_provider
from .processing import ProcessResult

logger = logging.getLogger(__name__)

_BLANK_DPI = 50
_BARCODE_DPI = 200

# fn(pdf, page index) -> None for a content page, or the separator's value
# ("" for a blank page or a barcode without text).
Detector = Callable[[bytes, int], Optional[str]]


class SplitUnavailable(RuntimeError):
    """A library the splitter needs is missing. The scan stays one document."""


def _blank(pdf: bytes, index: int) -> Optional[str]:
    return "" if pages.is_blank(pages._render(pdf, index, scale=_BLANK_DPI / 72)) else None


def read_barcodes(img) -> list[str]:
    """Values of every barcode and QR code in a PIL image."""
    try:
        import zxingcpp
    except ImportError as e:
        raise SplitUnavailable("Barcode reading is not installed, so the scan was not "
                               "split.") from e
    return [b.text for b in zxingcpp.read_barcodes(img.convert("L"))]


def _barcode(pdf: bytes, index: int) -> Optional[str]:
    values = read_barcodes(pages._render(pdf, index, scale=_BARCODE_DPI / 72))
    return values[0] if values else None


def split(pdf: bytes, result: ProcessResult, detect: Detector) -> list[tuple[bytes, ProcessResult]]:
    """Cut the PDF at every separator page. Separator pages are dropped.

    Each document's ProcessResult gets its own page_texts and, from the
    separator before it, a `barcode` value.
    """
    reader = PdfReader(BytesIO(pdf))
    groups: list[tuple[str, list[int]]] = [("", [])]
    for i in range(len(reader.pages)):
        try:
            value = detect(pdf, i)
        except SplitUnavailable:
            raise
        except Exception as e:
            logger.warning("Could not check page %d for a separator: %s", i + 1, e)
            value = None
        if value is None:
            groups[-1][1].append(i)
        else:
            groups.append((value, []))
    groups = [(v, idx) for v, idx in groups if idx]
    separators = len(reader.pages) - sum(len(idx) for _, idx in groups)
    if not separators or not groups:
        return [(pdf, result)]

    archive = _is_pdfa(pdf)
    docs = []
    for value, indexes in groups:
        writer = PdfWriter()
        for i in indexes:
            writer.add_page(reader.pages[i])
        buf = BytesIO()
        writer.write(buf)
        part = buf.getvalue()
        if archive:
            part = _make_pdfa(part)
        texts = [result.page_texts[i] for i in indexes] if result.page_texts else []
        doc = replace(result, pdf=part, page_texts=texts, warnings=list(result.warnings))
        doc.barcode = value  # read by the {barcode} name field
        docs.append((part, doc))
    logger.info("Split the scan into %d document(s) at %d separator page(s)",
                len(docs), separators)
    return docs


def _is_pdfa(pdf: bytes) -> bool:
    try:
        from .processing import pdfa
    except ImportError:
        return False
    return pdfa.is_pdfa(pdf)


def _make_pdfa(pdf: bytes) -> bytes:
    from .processing import pdfa

    return pdfa.make_pdfa(pdf)


def split_on_blank_pages(pdf: bytes, result: ProcessResult):
    return split(pdf, result, _blank)


def split_on_barcodes(pdf: bytes, result: ProcessResult):
    return split(pdf, result, _barcode)


@register_field_provider
def barcode_field(result, context) -> dict:
    """{barcode}: the value on the separator sheet before this document."""
    return {"barcode": getattr(result, "barcode", "") or ""}


register_splitter("blank", split_on_blank_pages)
register_splitter("barcode", split_on_barcodes)
