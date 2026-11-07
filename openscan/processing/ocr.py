"""Stage: text recognition. Makes a searchable PDF and fills result.page_texts.

Tesseract reads each scanned page and writes an invisible text layer. That
layer goes over the original page, so the scan image is not changed. A page
that already has text (a born-digital PDF) keeps it and is not read again.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

from pypdf import PdfReader, PdfWriter, Transformation

from . import _raster, tesseract
from .options import ProcessOptions, ProcessResult
from .registry import StageUnavailable, register_stage

logger = logging.getLogger(__name__)

ORDER = 50
MIN_DPI, MAX_DPI = 200, 400  # Tesseract reads best near 300 dpi
WORKERS = max(1, min(4, os.cpu_count() or 1))


def _read_page(png: str, base: str, dpi: int, langs: str) -> tuple[bytes, str]:
    """Run Tesseract on one page image. Returns (text-only PDF, plain text)."""
    proc = tesseract.run([png, base, "-l", langs, "--dpi", str(dpi), "--psm", "3",
                          "-c", "textonly_pdf=1", "pdf", "txt"])
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError(err[-1] if err else f"tesseract exit code {proc.returncode}")
    with open(base + ".pdf", "rb") as f:
        layer = f.read()
    with open(base + ".txt", "rb") as f:
        text = f.read().decode("utf-8", "replace")
    return layer, text.replace("\f", "").strip()


def ocr(pdf: bytes, options: ProcessOptions, result: ProcessResult) -> bytes:
    if not tesseract.find():
        raise StageUnavailable(tesseract.MISSING)
    langs, missing = tesseract.pick_languages(options.ocr_languages)
    if not langs:
        raise StageUnavailable("Text recognition has no language data for "
                               f"{', '.join(missing) or 'the chosen languages'}, so this step "
                               "was skipped.")
    if missing:
        result.warnings.append(f"Text recognition has no data for {', '.join(missing)}, so it "
                               f"used {langs.replace('+', ', ')} only.")

    reader = PdfReader(BytesIO(pdf))
    texts: list[str] = [""] * len(reader.pages)
    layers: dict[int, bytes] = {}
    failed = 0
    with tempfile.TemporaryDirectory(prefix="openscan-ocr-") as tmp:
        jobs = {}
        for i, page in enumerate(reader.pages):
            if _raster.has_text(page):
                texts[i] = (page.extract_text() or "").strip()
                continue
            dpi = min(MAX_DPI, max(MIN_DPI, _raster.native_dpi(page)))
            png = os.path.join(tmp, f"p{i}.png")
            _raster.render(pdf, i, dpi).convert("L").save(png)
            jobs[i] = (png, os.path.join(tmp, f"p{i}"), dpi)
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            futures = {i: pool.submit(_read_page, png, base, dpi, langs)
                       for i, (png, base, dpi) in jobs.items()}
            for i, fut in futures.items():
                try:
                    layers[i], texts[i] = fut.result()
                except (OSError, RuntimeError, subprocess.SubprocessError) as e:
                    failed += 1
                    logger.warning("Text recognition of page %d failed: %s", i + 1, e)

    result.page_texts = texts
    if failed:
        result.warnings.append(f"Text recognition failed on {failed} page(s); those pages "
                               "are not searchable.")
    if not layers:
        return pdf
    writer = PdfWriter(clone_from=reader)
    for i, layer in layers.items():
        page = writer.pages[i]
        _raster.clear_rotation(page)
        text_page = PdfReader(BytesIO(layer)).pages[0]
        shown = page.cropbox
        sx = float(shown.width) / float(text_page.mediabox.width)
        sy = float(shown.height) / float(text_page.mediabox.height)
        page.merge_transformed_page(
            text_page,
            Transformation().scale(sx, sy).translate(float(shown.left), float(shown.bottom)))
    return _raster.write(writer)


register_stage("ocr", ORDER, ocr, lambda o: bool(o.ocr))
