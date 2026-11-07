"""Stage: PDF/A-2b, the archive form of PDF.

A scan from OpenScanHub has only images and, after text recognition, an
embedded text font. To make it PDF/A-2b this stage adds what the standard
asks for: an sRGB output intent (the color profile comes from Pillow's
LittleCMS), XMP metadata that names the PDF/A part and matches the
document information, a file identifier and a PDF 1.7 header. It uses no
external program. Ghostscript is not used.

Caution: it does not convert content that PDF/A forbids. A PDF that comes
in with JavaScript, encryption or fonts that are not embedded is not made
valid by this stage.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from io import BytesIO
from xml.sax.saxutils import escape

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DictionaryObject,
    NameObject,
    NumberObject,
    StreamObject,
    TextStringObject,
)

from . import _raster
from .options import ProcessOptions, ProcessResult
from .registry import register_stage

logger = logging.getLogger(__name__)

ORDER = 70
PRODUCER = "OpenScanHub"
_ICC_NAME = "sRGB IEC61966-2.1"
_REMOVE_FROM_CATALOG = ("/AA", "/OpenAction", "/Perms")


def _srgb_icc() -> bytes:
    from PIL import ImageCms

    return ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()


def _xmp(now: datetime) -> bytes:
    stamp = now.strftime("%Y-%m-%dT%H:%M:%S+00:00")
    body = f"""<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:pdfaid="http://www.aiim.org/pdfa/ns/id/"
    xmlns:pdf="http://ns.adobe.com/pdf/1.3/"
    xmlns:xmp="http://ns.adobe.com/xap/1.0/">
   <pdfaid:part>2</pdfaid:part>
   <pdfaid:conformance>B</pdfaid:conformance>
   <pdf:Producer>{escape(PRODUCER)}</pdf:Producer>
   <xmp:CreateDate>{stamp}</xmp:CreateDate>
   <xmp:ModifyDate>{stamp}</xmp:ModifyDate>
   <xmp:MetadataDate>{stamp}</xmp:MetadataDate>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
    return body.encode("utf-8")


def is_pdfa(pdf: bytes) -> bool:
    """True when the PDF says it is PDF/A (it has an output intent and PDF/A XMP)."""
    try:
        root = PdfReader(BytesIO(pdf)).trailer["/Root"].get_object()
        meta = root.get("/Metadata")
        return "/OutputIntents" in root and meta is not None and \
            b"pdfaid:part" in meta.get_object().get_data()
    except Exception:
        return False


def make_pdfa(pdf: bytes) -> bytes:
    reader = PdfReader(BytesIO(pdf))
    if reader.is_encrypted:
        raise ValueError("the PDF is encrypted")
    writer = PdfWriter(clone_from=reader)
    root = writer.root_object
    for key in _REMOVE_FROM_CATALOG:
        if key in root:
            del root[key]

    icc = StreamObject()
    icc.set_data(_srgb_icc())
    icc[NameObject("/N")] = NumberObject(3)
    intent = DictionaryObject({
        NameObject("/Type"): NameObject("/OutputIntent"),
        NameObject("/S"): NameObject("/GTS_PDFA1"),
        NameObject("/OutputConditionIdentifier"): TextStringObject(_ICC_NAME),
        NameObject("/Info"): TextStringObject(_ICC_NAME),
        NameObject("/DestOutputProfile"): writer._add_object(icc),
    })
    root[NameObject("/OutputIntents")] = ArrayObject([writer._add_object(intent)])

    now = datetime.now(timezone.utc).replace(microsecond=0)
    date = now.strftime("D:%Y%m%d%H%M%S+00'00'")
    writer.metadata = {"/Producer": PRODUCER, "/CreationDate": date, "/ModDate": date}
    meta = StreamObject()
    meta.set_data(_xmp(now))
    meta[NameObject("/Type")] = NameObject("/Metadata")
    meta[NameObject("/Subtype")] = NameObject("/XML")
    root[NameObject("/Metadata")] = writer._add_object(meta)

    writer.pdf_header = "%PDF-1.7"
    writer.generate_file_identifiers()
    return _raster.write(writer)


def pdfa(pdf: bytes, options: ProcessOptions, result: ProcessResult) -> bytes:
    return make_pdfa(pdf)


register_stage("pdfa", ORDER, pdfa, lambda o: bool(o.pdfa))
