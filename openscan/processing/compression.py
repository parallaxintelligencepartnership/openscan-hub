"""Stage: compression. Makes the scan images smaller in place.

"standard" stores color and gray images as JPEG at quality 75, at most
300 dpi. "small" uses quality 50 and at most 150 dpi. 1-bit images stay as
they are. An image is replaced only when the new one is smaller, and the
text layer is kept.
"""

from __future__ import annotations

import logging
from io import BytesIO

from PIL import Image
from pypdf import PdfReader, PdfWriter

from . import _raster
from .options import ProcessOptions, ProcessResult
from .registry import register_stage

logger = logging.getLogger(__name__)

ORDER = 60
LEVELS = {"standard": (300, 75), "small": (150, 50)}  # max dpi, JPEG quality


def _encoded_size(obj) -> int:
    data = getattr(obj, "_data", None)  # the stream as stored in the file
    if data is None:
        data = obj.get_data()
    return len(data)


def _jpeg(img: Image.Image, quality: int) -> bytes:
    buf = BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


def compress(pdf: bytes, options: ProcessOptions, result: ProcessResult) -> bytes:
    max_dpi, quality = LEVELS[options.compression]
    writer = PdfWriter(clone_from=PdfReader(BytesIO(pdf)))
    done: set[int] = set()
    replaced = 0
    for page in writer.pages:
        width_pt = float(page.mediabox.width) or 1.0
        for image in page.images:
            ref = image.indirect_reference
            if ref is None or ref.idnum in done:
                continue
            done.add(ref.idnum)
            obj = ref.get_object()
            if (obj.get("/ImageMask") or int(obj.get("/BitsPerComponent", 8)) == 1
                    or "/SMask" in obj or "/Mask" in obj):
                continue
            img = image.image
            if img is None or img.mode not in ("L", "RGB", "P", "LA", "RGBA"):
                continue
            img = img.convert("L" if img.mode in ("L", "LA") else "RGB")
            dpi = img.width * 72.0 / width_pt
            if dpi > max_dpi * 1.05:
                f = max_dpi / dpi
                img = img.resize((max(1, round(img.width * f)), max(1, round(img.height * f))),
                                 Image.LANCZOS)
            if len(_jpeg(img, quality)) >= _encoded_size(obj):
                continue
            image.replace(img, quality=quality)
            replaced += 1
    if not replaced:
        return pdf
    for page in writer.pages:
        page.compress_content_streams()
    writer.compress_identical_objects()
    out = _raster.write(writer)
    if len(out) >= len(pdf):
        return pdf
    logger.info("Compressed %d image(s): %d -> %d bytes", replaced, len(pdf), len(out))
    return out


register_stage("compression", ORDER, compress, lambda o: o.compression in LEVELS)
