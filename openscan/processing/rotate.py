"""Stage: turn sideways and upside-down pages upright (0, 90, 180 or 270 degrees).

Tesseract's orientation detection finds the angle. The page gets a /Rotate
value, so the scan image itself is not changed.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import tempfile
from io import BytesIO

from pypdf import PdfReader, PdfWriter

from . import _raster, tesseract
from .options import ProcessOptions, ProcessResult
from .registry import StageUnavailable, register_stage

logger = logging.getLogger(__name__)

ORDER = 10
# Tesseract's orientation confidence below this is a guess; leave the page.
MIN_CONFIDENCE = 2.0
_ROTATE_RE = re.compile(r"^Rotate:\s*(\d+)", re.M)
_CONF_RE = re.compile(r"^Orientation confidence:\s*([\d.]+)", re.M)


def detect_rotation(png_path: str) -> int:
    """Degrees to turn the image clockwise to make it upright (0 when unsure)."""
    proc = tesseract.run([png_path, "stdout", "--psm", "0", "-l", "osd"])
    text = (proc.stdout or b"").decode("utf-8", "replace")
    rotate, conf = _ROTATE_RE.search(text), _CONF_RE.search(text)
    if proc.returncode != 0 or not rotate or not conf:
        # "Too few characters" and similar: no text to judge by.
        return 0
    angle = int(rotate.group(1)) % 360
    if angle not in (90, 180, 270) or float(conf.group(1)) < MIN_CONFIDENCE:
        return 0
    return angle


def auto_rotate(pdf: bytes, options: ProcessOptions, result: ProcessResult) -> bytes:
    if not tesseract.find():
        raise StageUnavailable("Turning pages upright needs the free Tesseract program, and "
                               "it is not installed, so this step was skipped.")
    if "osd" not in tesseract.languages():
        raise StageUnavailable("Turning pages upright needs Tesseract's orientation data "
                               "(osd), and it is not installed, so this step was skipped.")
    reader = PdfReader(BytesIO(pdf))
    turns: dict[int, int] = {}
    with tempfile.TemporaryDirectory(prefix="openscan-osd-") as tmp:
        for i, page in enumerate(reader.pages):
            if _raster.has_text(page):
                continue
            dpi = min(300, max(150, _raster.native_dpi(page)))
            path = os.path.join(tmp, f"p{i}.png")
            _raster.render(pdf, i, dpi).convert("L").save(path)
            try:
                angle = detect_rotation(path)
            except (OSError, subprocess.SubprocessError) as e:
                logger.warning("Orientation check of page %d failed: %s", i + 1, e)
                continue
            if angle:
                turns[i] = angle
    if not turns:
        return pdf
    writer = PdfWriter(clone_from=reader)
    for i, angle in turns.items():
        writer.pages[i].rotate(angle)
    logger.info("Turned %d page(s) upright", len(turns))
    return _raster.write(writer)


register_stage("auto_rotate", ORDER, auto_rotate, lambda o: bool(o.auto_rotate))
