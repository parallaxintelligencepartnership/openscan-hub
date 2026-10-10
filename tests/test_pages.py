import random
from io import BytesIO

from PIL import Image, ImageDraw
from pypdf import PdfReader

from openscan import pages


def _white(w=850, h=1100):
    return Image.new("RGB", (w, h), "white")


def _text_page(w=850, h=1100):
    img = _white(w, h)
    d = ImageDraw.Draw(img)
    for y in range(150, h - 150, 40):
        d.rectangle((120, y, w - 120, y + 14), fill="black")
    return img


def _noisy_blank(w=850, h=1100):
    """White paper with scanner speckle and dark edge shadows."""
    img = _white(w, h)
    px = img.load()
    rnd = random.Random(1)
    for _ in range(400):
        px[rnd.randrange(w), rnd.randrange(h)] = (90, 90, 90)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 20, h), fill="black")
    d.rectangle((0, h - 15, w, h), fill=(60, 60, 60))
    return img


def _pdf(images):
    out = BytesIO()
    images[0].save(out, format="PDF", resolution=100, save_all=True,
                   append_images=images[1:])
    return out.getvalue()


def test_is_blank_white_and_noisy():
    assert pages.is_blank(_white())
    assert pages.is_blank(_noisy_blank())
    assert pages.is_blank(_white().convert("1"))


def test_is_blank_rejects_text_and_light_marks():
    assert not pages.is_blank(_text_page())
    img = _white()
    ImageDraw.Draw(img).rectangle((300, 300, 500, 400), fill=(150, 150, 150))
    assert not pages.is_blank(img)


def test_drop_blank_pages_three_page_pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(_pdf([_text_page(), _noisy_blank(), _text_page()]))
    assert pages.drop_blank_pages(p) == 1
    assert len(PdfReader(str(p)).pages) == 2
    assert pages.drop_blank_pages(p) == 0


def test_drop_blank_keeps_one_page_when_all_blank():
    data, removed = pages.drop_blank_pages_bytes(_pdf([_white(), _white()]))
    assert removed == 1
    assert len(PdfReader(BytesIO(data)).pages) == 1


def test_render_thumbnail(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(_pdf([_text_page(), _white()]))
    out = tmp_path / "t.jpg"
    pages.render_thumbnail(p, 1, out, max_px=200)
    img = Image.open(out)
    assert img.format == "JPEG" and max(img.size) in (199, 200, 201)


def test_combine_mixed_pdf_and_jpeg():
    jpg = BytesIO()
    _text_page().save(jpg, format="JPEG")
    merged = pages.combine_pages([_pdf([_text_page(), _white()]), jpg.getvalue()])
    assert len(PdfReader(BytesIO(merged)).pages) == 3


def test_no_fitz_import_anywhere():
    import pathlib
    root = pathlib.Path(pages.__file__).parent
    for f in root.rglob("*.py"):
        text = f.read_text()
        assert "import fitz" not in text, f


def test_drop_blank_pages_keeps_normal_file_mode(tmp_path, monkeypatch):
    import os
    import stat
    if os.name == "nt":
        return
    from openscan import fileperm
    monkeypatch.setattr(fileperm, "_UMASK", 0o022)
    p = tmp_path / "doc.pdf"
    p.write_bytes(_pdf([_text_page(), _noisy_blank()]))
    assert pages.drop_blank_pages(p) == 1
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o644
