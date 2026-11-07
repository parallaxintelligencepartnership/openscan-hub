"""Processing pipeline: options, stage order, failure handling, blank pages."""

from io import BytesIO

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from openscan import pages, processing
from openscan.processing import ProcessOptions, StageUnavailable, registry


def _pdf(*blanks) -> bytes:
    frames = []
    for blank in blanks:
        img = Image.new("RGB", (600, 800), "white")
        if not blank:
            ImageDraw.Draw(img).rectangle((100, 100, 500, 700), fill="black")
        frames.append(img)
    buf = BytesIO()
    frames[0].save(buf, format="PDF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


@pytest.fixture
def stages(monkeypatch):
    monkeypatch.setattr(registry, "_stages", dict(registry._stages))
    return registry._stages


def test_defaults_are_all_off_and_leave_bytes_alone():
    opts = ProcessOptions()
    assert opts.to_dict() == {"ocr": False, "ocr_languages": ["eng"], "deskew": False,
                              "auto_rotate": False, "remove_blank_pages": False, "crop": False,
                              "compression": "none", "output_color": "keep", "pdfa": False}
    assert processing.enabled_stages(opts) == []
    res = processing.run(b"%PDF-not-really", opts)
    assert res.pdf == b"%PDF-not-really" and res.warnings == [] and res.pages_removed == 0


def test_from_dict_ignores_bad_values():
    opts = ProcessOptions.from_dict({"ocr": "yes", "compression": "huge", "deskew": True,
                                     "ocr_languages": ["deu"], "nope": 1})
    assert opts.ocr is False and opts.compression == "none"
    assert opts.deskew is True and opts.ocr_languages == ["deu"]
    assert ProcessOptions.from_dict(None) == ProcessOptions()


def test_blank_page_stage_removes_pages():
    res = processing.run(_pdf(False, True, False), ProcessOptions(remove_blank_pages=True))
    assert res.pages_removed == 1 and res.warnings == []
    assert len(PdfReader(BytesIO(res.pdf)).pages) == 2


def test_failing_stage_adds_warning_and_keeps_previous_bytes(stages):
    def upper(pdf, opts, result):
        return pdf + b"-a"

    def broken(pdf, opts, result):
        result.pages_removed += 3
        raise RuntimeError("disk on fire")

    def missing(pdf, opts, result):
        raise StageUnavailable("Text recognition is not installed.")

    processing.register_stage("first", 1, upper, lambda o: True)
    processing.register_stage("broken_step", 2, broken, lambda o: True)
    processing.register_stage("ocr_test", 3, missing, lambda o: True)
    processing.register_stage("off", 4, upper, lambda o: False)
    res = processing.run(b"%PDF", ProcessOptions())
    assert res.pdf == b"%PDF-a"
    assert res.pages_removed == 0
    assert len(res.warnings) == 2
    assert "broken step" in res.warnings[0] and "disk on fire" in res.warnings[0]
    assert res.warnings[1] == "Text recognition is not installed."


def test_stage_order(stages):
    seen = []
    processing.register_stage("late", 90, lambda p, o, r: seen.append("late") or p, lambda o: True)
    processing.register_stage("early", 5, lambda p, o, r: seen.append("early") or p, lambda o: True)
    processing.run(b"%PDF", ProcessOptions())
    assert seen.index("early") < seen.index("late")


def test_blank_removal_on_a_broken_pdf_is_a_warning():
    res = processing.run(b"%PDF-1.4 broken", ProcessOptions(remove_blank_pages=True))
    assert res.pdf == b"%PDF-1.4 broken"
    assert res.warnings and "remove blank pages" in res.warnings[0]


def test_finish_scan_still_works_for_old_callers():
    out = pages.finish_scan(_pdf(False, True), remove_blank_pages=True)
    assert len(PdfReader(BytesIO(out)).pages) == 1
