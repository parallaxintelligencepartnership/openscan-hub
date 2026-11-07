"""Helpers for the image stages: find scanned pages, render them, rebuild them."""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Callable, Optional

from PIL import Image, ImageChops
from pypdf import PageObject, PdfReader, PdfWriter

from .. import pages

logger = logging.getLogger(__name__)

DEFAULT_DPI = 300
MIN_DPI, MAX_DPI = 72, 600
_GRAY_TOLERANCE = 12


def has_text(page: PageObject) -> bool:
    """True when the page already has a text layer (born digital or OCR done)."""
    try:
        return len((page.extract_text() or "").strip()) >= 3
    except Exception:
        return False


def _page_images(page: PageObject) -> list:
    out = []
    resources = page.get("/Resources")
    xobjects = resources.get_object().get("/XObject") if resources else None
    for ref in (xobjects.get_object().values() if xobjects else []):
        obj = ref.get_object()
        if obj.get("/Subtype") == "/Image":
            out.append(obj)
    return out


def image_kinds(page: PageObject) -> set[str]:
    """The kinds of image on the page: "bw" (1-bit), "gray" or "color"."""
    kinds = set()
    try:
        for obj in _page_images(page):
            if obj.get("/ImageMask") or int(obj.get("/BitsPerComponent", 8)) == 1:
                kinds.add("bw")
            elif obj.get("/ColorSpace") == "/DeviceGray":
                kinds.add("gray")
            else:
                kinds.add("color")
    except Exception:
        kinds.add("color")
    return kinds


def native_dpi(page: PageObject) -> int:
    """The resolution of the largest image on the page (the scan), or 300."""
    best = 0.0
    try:
        width_pt = float(page.mediabox.width)
        for obj in _page_images(page):
            if width_pt > 0:
                best = max(best, float(obj.get("/Width", 0)) * 72.0 / width_pt)
    except Exception as e:
        logger.debug("Could not read the image resolution: %s", e)
    if best <= 0:
        return DEFAULT_DPI
    return int(round(min(MAX_DPI, max(MIN_DPI, best))))


def render(pdf: bytes, index: int, dpi: float) -> Image.Image:
    """Render one page as RGB at `dpi`, as it is shown (rotation and crop applied)."""
    return pages._render(pdf, index, scale=dpi / 72.0)


def drop_color_if_gray(img: Image.Image) -> Image.Image:
    """Return a gray image when the RGB render has no color (a gray scan)."""
    if img.mode != "RGB":
        return img
    small = img.resize((max(1, img.width // 8), max(1, img.height // 8)))
    r, g, b = small.split()
    # JPEG rounding leaves a few levels of difference in a gray scan.
    for a, c in ((r, g), (g, b), (r, b)):
        if ImageChops.difference(a, c).getextrema()[1] > _GRAY_TOLERANCE:
            return img
    return img.convert("L")


def image_page(img: Image.Image, dpi: float, quality: int = 90) -> PageObject:
    """A one-page PDF that shows `img` at `dpi`. A mode "1" image stays 1-bit."""
    if img.mode not in ("1", "L", "RGB"):
        img = img.convert("RGB")
    buf = BytesIO()
    extra = {} if img.mode == "1" else {"quality": quality}  # 1-bit is CCITT G4, not JPEG
    img.save(buf, format="PDF", resolution=float(dpi), **extra)
    return PdfReader(BytesIO(buf.getvalue())).pages[0]


def rebuild(pdf: bytes, fn: Callable[[int, PageObject], Optional[PageObject]]) -> tuple[bytes, int]:
    """Call fn(index, page) for every page. A returned page replaces the old one.

    Returns (pdf, pages changed). With no change the input bytes come back.
    """
    reader = PdfReader(BytesIO(pdf))
    writer = PdfWriter()
    changed = 0
    for i, page in enumerate(reader.pages):
        new = fn(i, page)
        if new is not None:
            changed += 1
            writer.add_page(new)
        else:
            writer.add_page(page)
    if not changed:
        return pdf, 0
    return write(writer), changed


def write(writer: PdfWriter) -> bytes:
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def clear_rotation(page: PageObject) -> None:
    """Move a /Rotate value into the content, so page space equals what is shown."""
    if page.rotation % 360:
        page.transfer_rotation_to_content()
