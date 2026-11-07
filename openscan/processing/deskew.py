"""Stage: straighten pages that went through the scanner at a small angle.

Pure Pillow: the angle that makes the text lines most horizontal gives the
sharpest row profile (rows of ink, then rows of paper).
"""

from __future__ import annotations

import logging
from typing import Optional

from PIL import Image

from . import _raster
from .options import ProcessOptions, ProcessResult
from .registry import register_stage

logger = logging.getLogger(__name__)

ORDER = 15
MAX_ANGLE = 10.0  # degrees searched either way
MIN_ANGLE = 0.3  # smaller skew is left alone (re-drawing the page costs quality)
_DETECT_DPI = 100
_MIN_INK = 0.002  # fraction of dark pixels below which a page has nothing to align


def _profile_score(ink: Image.Image, angle: float) -> float:
    turned = ink.rotate(angle, resample=Image.BILINEAR, fillcolor=0)
    rows = turned.resize((1, turned.height), Image.BOX).tobytes()  # one gray byte per row
    return float(sum((b - a) ** 2 for a, b in zip(rows, rows[1:])))


def skew_angle(img: Image.Image) -> Optional[float]:
    """The angle (degrees, counter-clockwise) that straightens `img`, or None."""
    gray = img.convert("L")
    ink = gray.point(lambda v: 255 if v < 128 else 0)
    hist = ink.histogram()
    if hist[255] / max(1, sum(hist)) < _MIN_INK:
        return None
    steps = int(MAX_ANGLE)
    best = max((float(a) for a in range(-steps, steps + 1)), key=lambda a: _profile_score(ink, a))
    fine = [best + i / 10.0 for i in range(-10, 11)]
    return max(fine, key=lambda a: _profile_score(ink, a))


def deskew(pdf: bytes, options: ProcessOptions, result: ProcessResult) -> bytes:
    def fix(i, page):
        if _raster.has_text(page):
            return None
        angle = skew_angle(_raster.render(pdf, i, _DETECT_DPI))
        if angle is None or abs(angle) < MIN_ANGLE:
            return None
        dpi = _raster.native_dpi(page)
        img = _raster.drop_color_if_gray(_raster.render(pdf, i, dpi))
        white = 255 if img.mode == "L" else (255, 255, 255)
        straight = img.rotate(angle, resample=Image.BICUBIC, fillcolor=white)
        logger.info("Straightened page %d by %.1f degrees", i + 1, angle)
        return _raster.image_page(straight, dpi)

    out, _ = _raster.rebuild(pdf, fix)
    return out


register_stage("deskew", ORDER, deskew, lambda o: bool(o.deskew))
