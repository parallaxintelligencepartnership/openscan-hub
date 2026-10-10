"""Page helpers: PDF conversion, merging, thumbnails and blank-page removal.

Rendering uses pypdfium2 (Apache-2.0/BSD). PyMuPDF is no longer used.
"""

import logging
import os
import tempfile
from io import BytesIO
from pathlib import Path
from typing import Union

from PIL import Image, ImageFilter
from pypdf import PdfReader, PdfWriter

from .fileperm import apply_default_mode

logger = logging.getLogger(__name__)

PdfSource = Union[str, os.PathLike, bytes]

# Pixels at or above this gray level count as "white" for blank detection.
_WHITE_LEVEL = 225
# Fraction of each edge cropped before blank detection (scanner shadows).
_MARGIN = 0.05
# Render resolution used for blank detection (low is enough and fast).
_BLANK_DPI = 50


def ensure_pdf(data: bytes, resolution: int = 300) -> bytes:
    """Return PDF bytes. Image data (JPEG, PNG, TIFF, BMP) is converted."""
    if data.startswith(b"%PDF-"):
        return data
    try:
        img = Image.open(BytesIO(data))
        frames = []
        for i in range(getattr(img, "n_frames", 1)):
            img.seek(i)
            frames.append(_pdf_safe(img.copy()))
    except Exception as e:
        logger.error("Failed to convert image to PDF: %s", e)
        raise ValueError(f"Data is not a valid PDF or image: {data[:20]!r}") from e
    out = BytesIO()
    frames[0].save(out, format="PDF", resolution=resolution, save_all=True,
                   append_images=frames[1:])
    return out.getvalue()


def _pdf_safe(img: Image.Image) -> Image.Image:
    if img.mode in ("1", "L", "RGB", "CMYK"):
        return img
    return img.convert("RGB")


def combine_pages(documents: list[bytes], resolution: int = 300) -> bytes:
    """Merge PDF and/or image documents, in order, into one PDF."""
    if not documents:
        raise ValueError("No pages to combine")
    pdfs = [ensure_pdf(d, resolution) for d in documents]
    if len(pdfs) == 1:
        return pdfs[0]
    writer = PdfWriter()
    for pdf in pdfs:
        for page in PdfReader(BytesIO(pdf)).pages:
            writer.add_page(page)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def page_count(pdf: PdfSource) -> int:
    return len(PdfReader(_as_stream(pdf)).pages)


def _as_stream(pdf: PdfSource):
    if isinstance(pdf, (bytes, bytearray)):
        return BytesIO(pdf)
    return str(pdf)


def _render(pdf: PdfSource, page_index: int, scale: float = None,
            max_px: int = None, width: int = None) -> Image.Image:
    """Render one page to a PIL image. Exactly one sizing option is used."""
    import pypdfium2 as pdfium

    src = bytes(pdf) if isinstance(pdf, (bytes, bytearray)) else str(pdf)
    doc = pdfium.PdfDocument(src)
    try:
        if len(doc) == 0:
            raise ValueError("PDF has no pages")
        if page_index < 0 or page_index >= len(doc):
            raise IndexError(f"Page index out of range: {page_index}")
        page = doc[page_index]
        try:
            w_pt, h_pt = page.get_size()
            if scale is None:
                if width:
                    scale = width / w_pt
                else:
                    scale = (max_px or 300) / max(w_pt, h_pt)
            bitmap = page.render(scale=scale)
            try:
                return bitmap.to_pil().convert("RGB")
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        doc.close()


def render_thumbnail(pdf_path, page_index: int, out_path, max_px: int = 300) -> str:
    """Render a JPEG thumbnail of one page; the longest side is max_px."""
    img = _render(pdf_path, page_index, max_px=max_px)
    img.save(str(out_path), format="JPEG", quality=80)
    return str(out_path)


def thumbnail_bytes(pdf: PdfSource, page_index: int = 0, max_px: int = 300,
                    width: int = None) -> bytes:
    """Same as render_thumbnail but in memory. `width` overrides max_px."""
    img = _render(pdf, page_index, max_px=max_px, width=width)
    out = BytesIO()
    img.save(out, format="JPEG", quality=80)
    return out.getvalue()


def is_blank(image: Image.Image, threshold: float = 0.995) -> bool:
    """True when at least `threshold` of the page is near-white.

    Margins are cropped (edge shadows) and a light blur removes paper
    texture and scanner noise before counting.
    """
    gray = image.convert("L")
    w, h = gray.size
    dx, dy = int(w * _MARGIN), int(h * _MARGIN)
    if w - 2 * dx > 0 and h - 2 * dy > 0:
        gray = gray.crop((dx, dy, w - dx, h - dy))
    gray = gray.filter(ImageFilter.GaussianBlur(radius=1))
    hist = gray.histogram()
    total = sum(hist)
    if total == 0:
        return True
    white = sum(hist[_WHITE_LEVEL:])
    return white / total >= threshold


def drop_blank_pages_bytes(pdf: bytes, threshold: float = 0.995) -> tuple[bytes, int]:
    """Remove blank pages from PDF bytes. Returns (pdf, pages_removed).

    At least one page is always kept so a document never becomes empty.
    """
    reader = PdfReader(BytesIO(pdf))
    n = len(reader.pages)
    if n == 0:
        return pdf, 0
    keep = []
    for i in range(n):
        try:
            blank = is_blank(_render(pdf, i, scale=_BLANK_DPI / 72), threshold)
        except Exception as e:
            logger.warning("Could not check page %d for blankness: %s", i + 1, e)
            blank = False
        if not blank:
            keep.append(i)
    if not keep:
        keep = [0]
    removed = n - len(keep)
    if removed == 0:
        return pdf, 0
    writer = PdfWriter()
    for i in keep:
        writer.add_page(reader.pages[i])
    out = BytesIO()
    writer.write(out)
    logger.info("Removed %d blank page(s) of %d", removed, n)
    return out.getvalue(), removed


def drop_blank_pages(pdf_path, threshold: float = 0.995) -> int:
    """Remove blank pages from a PDF file in place. Returns pages removed."""
    path = Path(pdf_path)
    data, removed = drop_blank_pages_bytes(path.read_bytes(), threshold)
    if removed:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                apply_default_mode(f.fileno())
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    return removed


def finish_scan(data: bytes, resolution: int = 300,
                remove_blank_pages: bool = False) -> bytes:
    """Normalize a finished scan to PDF and optionally drop blank pages."""
    pdf = ensure_pdf(data, resolution)
    if remove_blank_pages:
        try:
            pdf, _ = drop_blank_pages_bytes(pdf)
        except Exception as e:
            logger.warning("Blank page removal skipped: %s", e)
    return pdf
