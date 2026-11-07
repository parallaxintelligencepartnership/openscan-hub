"""Stage: output color. "gray" makes gray pages; "bw" makes true 1-bit pages.

A 1-bit page is stored with CCITT Group 4 compression, the format fax
machines use, so a text page becomes very small.
"""

from __future__ import annotations

import logging

from PIL import Image

from . import _raster
from .options import ProcessOptions, ProcessResult
from .registry import register_stage

logger = logging.getLogger(__name__)

ORDER = 40


def otsu_threshold(gray: Image.Image) -> int:
    """The gray level that best splits ink from paper (Otsu's method)."""
    hist = gray.histogram()[:256]
    total = sum(hist)
    if not total:
        return 128
    sum_all = sum(i * n for i, n in enumerate(hist))
    best, best_var, weight_b, sum_b = 128, -1.0, 0, 0.0
    for t in range(256):
        weight_b += hist[t]
        if weight_b == 0:
            continue
        weight_f = total - weight_b
        if weight_f == 0:
            break
        sum_b += t * hist[t]
        mean_b, mean_f = sum_b / weight_b, (sum_all - sum_b) / weight_f
        var = weight_b * weight_f * (mean_b - mean_f) ** 2
        if var > best_var:
            best, best_var = t, var
    return best


def to_bw(img: Image.Image) -> Image.Image:
    gray = img.convert("L")
    level = otsu_threshold(gray)
    return gray.point(lambda v: 255 if v > level else 0).convert("1", dither=Image.Dither.NONE)


def output_color(pdf: bytes, options: ProcessOptions, result: ProcessResult) -> bytes:
    mode = options.output_color

    def convert(i, page):
        if _raster.has_text(page):
            return None
        kinds = _raster.image_kinds(page)
        if kinds and kinds <= ({"bw"} if mode == "bw" else {"bw", "gray"}):
            return None  # already in the asked color
        dpi = _raster.native_dpi(page)
        img = _raster.render(pdf, i, dpi)
        img = to_bw(img) if mode == "bw" else img.convert("L")
        return _raster.image_page(img, dpi)

    out, changed = _raster.rebuild(pdf, convert)
    if changed:
        logger.info("Made %d page(s) %s", changed, "black and white" if mode == "bw" else "gray")
    return out


register_stage("output_color", ORDER, output_color,
               lambda o: o.output_color in ("gray", "bw"))
