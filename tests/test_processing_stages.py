"""The image and text stages on scan-like pages made in code."""

import hashlib
from io import BytesIO

import pytest
from PIL import Image, ImageStat
from pypdf import PdfWriter

from openscan import pages, processing
from openscan.processing import ProcessOptions, _raster, compression, deskew, pdfa, tesseract
from openscan.processing.crop import paper_box
from scanfixtures import DPI, blank_page, on_dark_background, reader, text_page, to_pdf

needs_tesseract = pytest.mark.skipif(tesseract.find() is None,
                                     reason="the tesseract program is not installed")


def _id(data: bytes) -> str:
    """A short stand-in for PDF bytes, so a failed comparison prints briefly."""
    return hashlib.sha256(data).hexdigest()[:16]


def _render(pdf, index=0, dpi=100):
    return pages._render(pdf, index, scale=dpi / 72)


def _ocr_text(img):
    """Read an image with Tesseract (for checking that a page is upright)."""
    import tempfile, os
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "p.png")
        img.convert("L").save(path)
        return tesseract.run([path, "stdout", "-l", "eng"]).stdout.decode()


def _no_tesseract(monkeypatch):
    monkeypatch.setattr(tesseract, "find", lambda: None)


def test_all_stages_are_registered_in_order():
    names = [s.name for s in processing.stages()]
    assert names == ["auto_rotate", "deskew", "crop", "remove_blank_pages", "output_color",
                     "ocr", "compression", "pdfa"]


# --- OCR ---------------------------------------------------------------

@needs_tesseract
def test_ocr_adds_text_layer_and_page_texts_and_keeps_the_image():
    pdf = to_pdf(text_page(), blank_page())
    res = processing.run(pdf, ProcessOptions(ocr=True))
    assert res.warnings == []
    assert len(res.page_texts) == 2
    assert "INVOICE 4711" in res.page_texts[0] and "ACME" in res.page_texts[0]
    assert res.page_texts[1] == ""
    out = reader(res.pdf)
    assert "INVOICE 4711" in out.pages[0].extract_text()
    assert _id(out.pages[0].images[0].data) == _id(reader(pdf).pages[0].images[0].data)
    assert [float(v) for v in out.pages[0].mediabox] == \
        [float(v) for v in reader(pdf).pages[0].mediabox]


@needs_tesseract
def test_ocr_keeps_an_existing_text_layer():
    first = processing.run(to_pdf(text_page()), ProcessOptions(ocr=True))
    again = processing.run(first.pdf, ProcessOptions(ocr=True))
    assert _id(again.pdf) == _id(first.pdf)
    assert "INVOICE 4711" in again.page_texts[0]


@needs_tesseract
def test_ocr_uses_the_installed_languages_and_warns_about_the_rest():
    res = processing.run(to_pdf(text_page()), ProcessOptions(ocr=True,
                                                             ocr_languages=["eng", "xyz"]))
    assert "INVOICE" in res.page_texts[0]
    assert res.warnings == ["Text recognition has no data for xyz, so it used eng only."]
    res = processing.run(to_pdf(text_page()), ProcessOptions(ocr=True, ocr_languages=["xyz"]))
    assert res.page_texts == [] and "no language data for xyz" in res.warnings[0]


def test_ocr_without_tesseract_is_a_plain_warning(monkeypatch):
    _no_tesseract(monkeypatch)
    pdf = to_pdf(text_page())
    res = processing.run(pdf, ProcessOptions(ocr=True, auto_rotate=True))
    assert _id(res.pdf) == _id(pdf)
    assert res.warnings[0].startswith("Turning pages upright needs the free Tesseract program")
    assert res.warnings[1] == tesseract.MISSING


def test_tesseract_is_found_in_the_windows_install_folder(monkeypatch, tmp_path):
    exe = tmp_path / "Tesseract-OCR" / "tesseract.exe"
    exe.parent.mkdir()
    exe.write_bytes(b"")
    monkeypatch.setattr(tesseract.shutil, "which", lambda name: None)
    monkeypatch.setattr(tesseract.sys, "platform", "win32")
    monkeypatch.setenv("ProgramFiles", str(tmp_path))
    assert tesseract.find() == str(exe)
    monkeypatch.setattr(tesseract.sys, "platform", "linux")
    assert tesseract.find() is None


def test_bad_language_names_never_reach_tesseract(monkeypatch):
    monkeypatch.setattr(tesseract, "languages", lambda: {"eng", "deu"})
    assert tesseract.pick_languages(["deu", "-c", "eng", "deu"]) == ("deu+eng", ["-c"])


# --- auto-rotate -------------------------------------------------------

@needs_tesseract
@pytest.mark.parametrize("turn,expected", [(90, 90), (180, 180), (270, 270)])
def test_auto_rotate_turns_pages_upright(turn, expected):
    page = text_page().rotate(turn, expand=True)  # Pillow turns counter-clockwise
    pdf = to_pdf(page)
    res = processing.run(pdf, ProcessOptions(auto_rotate=True))
    assert res.warnings == []
    out = reader(res.pdf).pages[0]
    assert out.rotation == expected
    assert _id(out.images[0].data) == _id(reader(pdf).pages[0].images[0].data)  # untouched
    assert "INVOICE" in _ocr_text(_render(res.pdf, dpi=200))


@needs_tesseract
def test_auto_rotate_leaves_upright_and_blank_pages_alone():
    pdf = to_pdf(text_page(), blank_page())
    assert _id(processing.run(pdf, ProcessOptions(auto_rotate=True)).pdf) == _id(pdf)


# --- deskew ------------------------------------------------------------

@pytest.mark.parametrize("skew", [3.0, -4.5])
def test_deskew_finds_and_removes_the_skew(skew):
    page = text_page().rotate(skew, resample=Image.BICUBIC, fillcolor="white")
    found = deskew.skew_angle(page.resize((page.width // 2, page.height // 2)))
    assert found == pytest.approx(-skew, abs=0.3)
    res = processing.run(to_pdf(page), ProcessOptions(deskew=True))
    assert res.warnings == []
    after = deskew.skew_angle(_render(res.pdf))
    assert abs(after) < deskew.MIN_ANGLE
    out = reader(res.pdf).pages[0]
    assert out.mediabox.width == pytest.approx(8.5 * 72, abs=1)
    assert _raster.image_kinds(out) == {"gray"}  # a gray-looking page stays gray


def test_deskew_leaves_straight_and_blank_pages_alone():
    pdf = to_pdf(text_page(), blank_page())
    assert _id(processing.run(pdf, ProcessOptions(deskew=True)).pdf) == _id(pdf)


# --- crop --------------------------------------------------------------

def test_crop_cuts_to_the_paper_edge():
    scan = on_dark_background(text_page())
    pdf = to_pdf(scan)
    res = processing.run(pdf, ProcessOptions(crop=True))
    assert res.warnings == []
    box = reader(res.pdf).pages[0].cropbox
    assert float(box.width) == pytest.approx(8.5 * 72, abs=4)
    assert float(box.height) == pytest.approx(11 * 72, abs=4)
    shown = _render(res.pdf).convert("L")
    w, h = shown.size
    for edge in ((0, 0, w, 3), (0, h - 3, w, h), (0, 0, 3, h), (w - 3, 0, w, h)):
        assert ImageStat.Stat(shown.crop(edge)).mean[0] > 200  # no dark lid left
    assert _id(reader(res.pdf).pages[0].images[0].data) == \
        _id(reader(pdf).pages[0].images[0].data)


def test_crop_works_on_a_turned_page():
    scan = on_dark_background(text_page())
    writer = PdfWriter(clone_from=reader(to_pdf(scan)))
    writer.pages[0].rotate(90)
    buf = BytesIO()
    writer.write(buf)
    res = processing.run(buf.getvalue(), ProcessOptions(crop=True))
    shown = _render(res.pdf).convert("L")
    assert shown.width == pytest.approx(11 * 100, abs=8)  # turned: wide page
    assert shown.height == pytest.approx(8.5 * 100, abs=8)
    w, h = shown.size
    for edge in ((0, 0, w, 3), (0, h - 3, w, h), (0, 0, 3, h), (w - 3, 0, w, h)):
        assert ImageStat.Stat(shown.crop(edge)).mean[0] > 200


def test_crop_keeps_a_page_with_white_margins():
    assert paper_box(text_page()) is None
    pdf = to_pdf(text_page(), blank_page())
    assert _id(processing.run(pdf, ProcessOptions(crop=True)).pdf) == _id(pdf)


# --- output color ------------------------------------------------------

def test_gray_output():
    pdf = to_pdf(text_page(color=True))
    assert _raster.image_kinds(reader(pdf).pages[0]) == {"color"}
    res = processing.run(pdf, ProcessOptions(output_color="gray"))
    assert res.warnings == []
    assert _raster.image_kinds(reader(res.pdf).pages[0]) == {"gray"}
    assert len(res.pdf) < len(pdf)


def test_bw_output_is_true_one_bit():
    pdf = to_pdf(text_page(color=True))
    res = processing.run(pdf, ProcessOptions(output_color="bw"))
    assert res.warnings == []
    image = reader(res.pdf).pages[0].images[0]
    obj = image.indirect_reference.get_object()
    assert obj["/BitsPerComponent"] == 1
    assert "/CCITTFaxDecode" in obj["/Filter"]  # one filter, in an array
    assert image.image.mode == "1"
    assert len(res.pdf) < len(pdf) / 5
    assert _id(res.pdf) == _id(processing.run(res.pdf, ProcessOptions(output_color="bw")).pdf)


# --- compression -------------------------------------------------------

def _photo_page():
    img = text_page(color=True)
    noise = Image.effect_noise(img.size, 40).convert("RGB")
    return Image.blend(img, noise, 0.25)


@pytest.mark.parametrize("level", ["standard", "small"])
def test_compression_makes_the_file_smaller(level):
    pdf = to_pdf(_photo_page(), dpi=DPI, quality=95)
    res = processing.run(pdf, ProcessOptions(compression=level))
    assert res.warnings == []
    assert len(res.pdf) < len(pdf) * 0.8
    image = reader(res.pdf).pages[0].images[0].image
    max_dpi = compression.LEVELS[level][0]
    assert image.width <= 8.5 * min(DPI, max_dpi) + 1


def test_small_is_smaller_than_standard_and_keeps_the_text_layer():
    pdf = to_pdf(_photo_page(), dpi=300, quality=95)
    with_text = _with_text_layer(pdf, "HELLO ARCHIVE")
    standard = processing.run(with_text, ProcessOptions(compression="standard"))
    small = processing.run(with_text, ProcessOptions(compression="small"))
    assert standard.warnings == [] and small.warnings == []
    standard, small = standard.pdf, small.pdf
    assert len(small) < len(standard) < len(with_text)
    assert "HELLO ARCHIVE" in reader(small).pages[0].extract_text()


def test_compression_leaves_one_bit_pages_alone():
    pdf = processing.run(to_pdf(text_page()), ProcessOptions(output_color="bw")).pdf
    assert _id(processing.run(pdf, ProcessOptions(compression="small")).pdf) == _id(pdf)


def _with_text_layer(pdf: bytes, text: str) -> bytes:
    """Put a plain Helvetica text line on page 1 (a born-digital text layer)."""
    from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter(clone_from=reader(pdf))
    page = writer.pages[0]
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    res = page["/Resources"].get_object()
    res[NameObject("/Font")] = DictionaryObject({NameObject("/FT"): writer._add_object(font)})
    stream = DecodedStreamObject()
    stream.set_data(f"BT /FT 12 Tf 72 72 Td ({text}) Tj ET".encode())
    contents = page.raw_get("/Contents")  # the reference, not the stream
    page[NameObject("/Contents")] = ArrayObject([contents, writer._add_object(stream)])
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


# --- PDF/A -------------------------------------------------------------

def test_pdfa_adds_output_intent_metadata_and_id():
    pdf = to_pdf(text_page())
    res = processing.run(pdf, ProcessOptions(pdfa=True))
    assert res.warnings == []
    assert res.pdf.startswith(b"%PDF-1.7")
    r = reader(res.pdf)
    root = r.trailer["/Root"].get_object()
    intent = root["/OutputIntents"][0].get_object()
    assert intent["/S"] == "/GTS_PDFA1"
    assert intent["/DestOutputProfile"].get_object()["/N"] == 3
    xmp = root["/Metadata"].get_object().get_data()
    assert b"<pdfaid:part>2</pdfaid:part>" in xmp and b"<pdfaid:conformance>B" in xmp
    assert r.metadata["/Producer"] == "OpenScanHub"
    assert len(r.trailer["/ID"]) == 2
    assert pdfa.is_pdfa(res.pdf) and not pdfa.is_pdfa(pdf)


# --- everything at once -----------------------------------------------

@needs_tesseract
def test_every_stage_together():
    scan = on_dark_background(text_page())
    scan = scan.rotate(2.5, resample=Image.BICUBIC, fillcolor=(55, 55, 60)).rotate(90, expand=True)
    pdf = to_pdf(scan, blank_page())
    opts = ProcessOptions(ocr=True, deskew=True, auto_rotate=True, remove_blank_pages=True,
                          crop=True, compression="small", output_color="bw", pdfa=True)
    res = processing.run(pdf, opts)
    assert res.warnings == []
    assert res.pages_removed == 1
    assert "INVOICE 4711" in res.page_texts[0]
    out = reader(res.pdf)
    assert len(out.pages) == 1
    assert "INVOICE 4711" in out.pages[0].extract_text()
    assert pdfa.is_pdfa(res.pdf)
    assert len(res.pdf) < len(pdf) / 5
