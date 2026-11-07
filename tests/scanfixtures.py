"""Scan-like test pages made in code: text rendered with Pillow, then turned,
skewed, put on a dark scanner background or given a barcode."""

from __future__ import annotations

from io import BytesIO

from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfReader

DPI = 200
LETTER = (int(8.5 * DPI), int(11 * DPI))

TEXT = [
    "INVOICE 4711",
    "ACME Paper Supplies Limited",
    "The quick brown fox jumps over the lazy dog.",
    "Please pay the amount below within thirty days.",
    "Thank you for your order and your business.",
    "Total amount due: 1234.56 dollars",
    "Questions about this invoice? Write to us.",
    "We ship every order on the next working day.",
]


def text_page(lines=TEXT, size=LETTER, color=False) -> Image.Image:
    img = Image.new("RGB", size, "white")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=36)
    y = 150
    for line in lines:
        draw.text((150, y), line, fill=(20, 40, 160) if color else "black", font=font)
        y += 70
    if color:
        draw.rectangle((150, y + 40, 900, y + 400), fill=(200, 30, 30))
    return img


def blank_page(size=LETTER) -> Image.Image:
    return Image.new("RGB", size, "white")


def barcode_page(value: str, kind: str = "QRCode", size=LETTER) -> Image.Image:
    import zxingcpp

    code = zxingcpp.create_barcode(value, getattr(zxingcpp.BarcodeFormat, kind))
    mark = Image.fromarray(zxingcpp.write_barcode_to_image(code, scale=6)).convert("RGB")
    img = blank_page(size)
    img.paste(mark, ((size[0] - mark.width) // 2, 400))
    ImageDraw.Draw(img).text((150, 200), "SEPARATOR SHEET", fill="black",
                             font=ImageFont.load_default(size=36))
    return img


def on_dark_background(page: Image.Image, margin=(120, 90, 200, 160)) -> Image.Image:
    """The page on a dark scanner lid, as a flatbed scan of a smaller sheet."""
    left, top, right, bottom = margin
    canvas = Image.new("RGB", (page.width + left + right, page.height + top + bottom),
                       (55, 55, 60))
    canvas.paste(page, (left, top))
    return canvas


def to_pdf(*images: Image.Image, dpi: int = DPI, quality: int = 75) -> bytes:
    buf = BytesIO()
    images[0].save(buf, format="PDF", resolution=float(dpi), save_all=True,
                   append_images=list(images[1:]), quality=quality)
    return buf.getvalue()


def reader(pdf: bytes) -> PdfReader:
    return PdfReader(BytesIO(pdf))
