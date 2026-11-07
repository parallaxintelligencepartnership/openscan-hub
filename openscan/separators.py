"""Separator sheets: split one feeder batch into several documents.

    blank    a blank page ends a document and is dropped
    barcode  a page with a code that starts with the separator prefix
             (ProcessOptions.separator_prefix, default "OSH:") ends a
             document and is dropped. The text after the prefix is the
             {barcode} name field of the next document. Pages with other
             codes, such as an invoice's QR code, are kept.

separator_sheet(label) makes a printable sheet for "OSH:<label>".

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

DEFAULT_PREFIX = "OSH:"
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


def _prefix(result: ProcessResult) -> str:
    options = getattr(result, "options", None)
    prefix = getattr(options, "separator_prefix", DEFAULT_PREFIX)
    return DEFAULT_PREFIX if prefix is None else str(prefix)


def _barcode_detector(prefix: str) -> Detector:
    def detect(pdf: bytes, index: int) -> Optional[str]:
        for value in read_barcodes(pages._render(pdf, index, scale=_BARCODE_DPI / 72)):
            if value.startswith(prefix):
                return value[len(prefix):].strip()
        return None
    return detect


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
    return split(pdf, result, _barcode_detector(_prefix(result)))


_SHEET_DPI = 200
_LETTER = (int(8.5 * _SHEET_DPI), int(11 * _SHEET_DPI))
MAX_LABEL = 80


def separator_sheet(label: str, prefix: str = DEFAULT_PREFIX) -> bytes:
    """A one-page Letter PDF: a large QR code for prefix+label, the label under it."""
    import zxingcpp
    from PIL import Image, ImageDraw, ImageFont

    label = " ".join(str(label).split())[:MAX_LABEL]
    code = zxingcpp.create_barcode(prefix + label, zxingcpp.BarcodeFormat.QRCode)
    mark = Image.fromarray(zxingcpp.write_barcode_to_image(code, scale=1)).convert("L")
    side = 4 * _SHEET_DPI  # 4 inches
    mark = mark.resize((side, side), Image.NEAREST)
    page = Image.new("L", _LETTER, 255)
    top = _SHEET_DPI * 2
    page.paste(mark, ((_LETTER[0] - side) // 2, top))
    draw = ImageDraw.Draw(page)
    for text, size, y in ((label, 72, top + side + 80),
                          ("OpenScanHub separator sheet", 36, top + side + 220)):
        try:
            font = ImageFont.load_default(size=size)
        except TypeError:  # Pillow without FreeType text sizes
            font = ImageFont.load_default()
        width = draw.textlength(text, font=font)
        draw.text(((_LETTER[0] - width) / 2, y), text, fill=0, font=font)
    out = BytesIO()
    page.convert("1").save(out, format="PDF", resolution=float(_SHEET_DPI))
    return out.getvalue()


@register_field_provider
def barcode_field(result, context) -> dict:
    """{barcode}: the value on the separator sheet before this document."""
    return {"barcode": getattr(result, "barcode", "") or ""}


register_splitter("blank", split_on_blank_pages)
register_splitter("barcode", split_on_barcodes)
