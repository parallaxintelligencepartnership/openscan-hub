"""Stage: crop a flatbed scan to the edge of the paper.

The scanner background has the color of the image border. The paper is
the area that is clearly lighter than it. The page box is made smaller; the scan
image is not changed.
"""

from __future__ import annotations

import logging
from io import BytesIO
from typing import Optional

from PIL import Image, ImageChops, ImageFilter
from pypdf import PdfReader, PdfWriter
from pypdf.generic import RectangleObject

from . import _raster
from .options import ProcessOptions, ProcessResult
from .registry import register_stage

logger = logging.getLogger(__name__)

ORDER = 20
_DETECT_DPI = 50
_DIFF = 40  # gray levels the paper is lighter than the background
_KEEP = 0.95  # crop only when the paper is smaller than this part of the page area
_MIN_PAPER = 0.05  # a smaller "paper" is noise, not paper


def _border_color(gray: Image.Image) -> int:
    w, h = gray.size
    edge = []
    for box in ((0, 0, w, 2), (0, h - 2, w, h), (0, 0, 2, h), (w - 2, 0, w, h)):
        edge.extend(gray.crop(box).tobytes())
    edge.sort()
    return edge[len(edge) // 2]


def paper_box(img: Image.Image) -> Optional[tuple[int, int, int, int]]:
    """Pixel box (left, top, right, bottom) of the paper, or None to keep all."""
    gray = img.convert("L").filter(ImageFilter.MedianFilter(3))
    w, h = gray.size
    if w < 10 or h < 10:
        return None
    background = _border_color(gray)
    # Paper is lighter than the background (a dark lid or feeder backing).
    # A page with white margins on a white lid stays as it is: its text is
    # darker than the border, not lighter.
    diff = ImageChops.subtract(gray, Image.new("L", gray.size, background))
    mask = diff.point(lambda v: 255 if v > _DIFF else 0)
    box = mask.getbbox()
    if not box:
        return None
    left, top, right, bottom = box
    area = (right - left) * (bottom - top)
    if area >= _KEEP * w * h or area < _MIN_PAPER * w * h:
        return None
    return left, top, right, bottom


def crop(pdf: bytes, options: ProcessOptions, result: ProcessResult) -> bytes:
    reader = PdfReader(BytesIO(pdf))
    boxes = {}
    for i, page in enumerate(reader.pages):
        if _raster.has_text(page):
            continue
        img = _raster.render(pdf, i, _DETECT_DPI)
        box = paper_box(img)
        if box:
            boxes[i] = (box, img.size)
    if not boxes:
        return pdf
    writer = PdfWriter(clone_from=reader)
    for i, ((left, top, right, bottom), (w, h)) in boxes.items():
        page = writer.pages[i]
        _raster.clear_rotation(page)  # page space now matches the render
        shown = page.cropbox
        sx, sy = float(shown.width) / w, float(shown.height) / h
        x0, y1 = float(shown.left), float(shown.top)
        rect = RectangleObject((x0 + left * sx, y1 - bottom * sy, x0 + right * sx, y1 - top * sy))
        page.mediabox = rect
        page.cropbox = rect
    logger.info("Cropped %d page(s) to the paper edge", len(boxes))
    return _raster.write(writer)


register_stage("crop", ORDER, crop, lambda o: bool(o.crop))
