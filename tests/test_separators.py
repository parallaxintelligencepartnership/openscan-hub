"""Separator sheets: blank and barcode splitters and the {barcode} name field."""

import sys
import types
from datetime import datetime
from io import BytesIO

from conftest import TOKEN

from openscan import intake, naming, pages, separators
from openscan.processing import ProcessOptions, ProcessResult, pdfa
from openscan.profiles import Profile
from pypdf import PdfWriter
from scanfixtures import barcode_page, blank_page, reader, text_page, to_pdf


def _page(label):
    return text_page([f"DOCUMENT {label}", "Some words on the page."])


def _labels(pdf):
    return [p.extract_text() for p in reader(pdf).pages]


def _profile(split):
    return types.SimpleNamespace(split=split)


def test_both_splitters_are_registered():
    assert intake._splitters["blank"] is separators.split_on_blank_pages
    assert intake._splitters["barcode"] is separators.split_on_barcodes


def test_blank_pages_split_the_batch_and_are_dropped():
    pdf = to_pdf(_page("A"), blank_page(), _page("B"), _page("C"), blank_page(), _page("D"),
                 blank_page())
    result = ProcessResult(pdf=pdf, page_texts=["A", "", "B", "C", "", "D", ""],
                           warnings=["w"])
    docs = intake._split(_profile("blank"), pdf, result)
    assert [len(reader(d).pages) for d, _ in docs] == [1, 2, 1]
    assert [r.page_texts for _, r in docs] == [["A"], ["B", "C"], ["D"]]
    assert all(r.pdf is d and r.warnings == ["w"] for d, r in docs)
    assert result.warnings == ["w"]


def test_a_batch_without_separators_stays_one_document():
    pdf = to_pdf(_page("A"), _page("B"))
    result = ProcessResult(pdf=pdf)
    for split in (separators.split_on_blank_pages, separators.split_on_barcodes):
        docs = split(pdf, result)
        assert len(docs) == 1 and docs[0][0] is pdf and docs[0][1] is result


def test_barcode_pages_split_the_batch_and_name_the_next_document():
    pdf = to_pdf(_page("A"), barcode_page("OSH:INV-2026-001"), _page("B"),
                 barcode_page("https://pay.example/invoice/42"), _page("C"),
                 barcode_page("OSH: Box 7/Shelf 2", kind="Code128"), _page("D"))
    docs = intake._split(_profile("barcode"), pdf, ProcessResult(pdf=pdf))
    # The invoice QR code (no prefix) is a normal page and stays.
    assert [len(reader(d).pages) for d, _ in docs] == [1, 3, 1]
    assert [r.barcode for _, r in docs] == ["", "INV-2026-001", "Box 7/Shelf 2"]
    names = []
    for _, r in docs:
        fields = naming.collect_fields(r, {"source": "adf", "now": datetime(2026, 11, 6)})
        names.append(naming.render("{barcode}_{date}", fields))
    assert names == ["unknown_2026-11-06.pdf", "INV-2026-001_2026-11-06.pdf",
                     "Box 7_Shelf 2_2026-11-06.pdf"]


def test_separators_at_the_start_and_twice_in_a_row():
    pdf = to_pdf(barcode_page("OSH:X1"), barcode_page("OSH:X2"), _page("A"))
    docs = separators.split_on_barcodes(pdf, ProcessResult(pdf=pdf))
    assert len(docs) == 1 and docs[0][1].barcode == "X2"
    assert len(reader(docs[0][0]).pages) == 1


def test_missing_barcode_library_keeps_one_document_with_a_warning(monkeypatch):
    pdf = to_pdf(_page("A"), barcode_page("OSH:X"), _page("B"))
    monkeypatch.setitem(sys.modules, "zxingcpp", None)
    result = ProcessResult(pdf=pdf)
    docs = intake._split(_profile("barcode"), pdf, result)
    assert len(docs) == 1 and docs[0][0] is pdf
    assert "Barcode reading is not installed" in result.warnings[0]


def test_a_split_pdfa_stays_pdfa():
    pdf = pdfa.make_pdfa(to_pdf(_page("A"), blank_page(), _page("B")))
    docs = separators.split_on_blank_pages(pdf, ProcessResult(pdf=pdf))
    assert len(docs) == 2 and all(pdfa.is_pdfa(d) for d, _ in docs)


def test_split_through_handle_scan(tmp_path, config_dir, monkeypatch):
    """A barcode-split profile delivers one named file per document."""
    from openscan import retry_queue
    from openscan.config import AppConfig

    q = retry_queue.RetryQueue(tmp_path / "q" / "retry_queue.json", tmp_path / "q" / "files")
    monkeypatch.setattr(retry_queue, "_queue", q)
    out_dir = tmp_path / "out"
    profile = Profile(id="b", name="B", split="barcode", name_rule="{barcode}_{n}",
                      destinations=[{"type": "folder", "options": {"folder": str(out_dir)}}])
    data = to_pdf(_page("A"), barcode_page("OSH:CASE-9"), _page("B"), _page("C"))
    try:
        res = intake.handle_scan(AppConfig(), data, source="adf", profile=profile)
    finally:
        q.stop()
    assert res.ok and [d.filename for d in res.documents] == ["unknown_001.pdf",
                                                              "CASE-9_002.pdf"]
    assert len(reader((out_dir / "CASE-9_002.pdf").read_bytes()).pages) == 2


def test_other_prefix_and_empty_prefix():
    pdf = to_pdf(_page("A"), barcode_page("SEP-1"), _page("B"))
    default = ProcessResult(pdf=pdf, options=ProcessOptions())
    assert len(separators.split_on_barcodes(pdf, default)) == 1
    custom = ProcessResult(pdf=pdf, options=ProcessOptions(separator_prefix="SEP-"))
    assert [r.barcode for _, r in separators.split_on_barcodes(pdf, custom)] == ["", "1"]
    any_code = ProcessResult(pdf=pdf, options=ProcessOptions(separator_prefix=""))
    assert [r.barcode for _, r in separators.split_on_barcodes(pdf, any_code)] == ["", "SEP-1"]


def test_blank_split_keeps_blank_sheets_for_the_splitter(tmp_path, config_dir, monkeypatch):
    """remove_blank_pages is on, but split=blank needs the blank sheets."""
    from openscan import retry_queue
    from openscan.config import AppConfig

    q = retry_queue.RetryQueue(tmp_path / "q" / "retry_queue.json", tmp_path / "q" / "files")
    monkeypatch.setattr(retry_queue, "_queue", q)
    out_dir = tmp_path / "out"
    profile = Profile(id="k", name="K", split="blank", name_rule="part_{n}",
                      processing=ProcessOptions(remove_blank_pages=True),
                      destinations=[{"type": "folder", "options": {"folder": str(out_dir)}}])
    data = to_pdf(_page("A"), blank_page(), _page("B"), _page("C"), blank_page())
    try:
        res = intake.handle_scan(AppConfig(), data, source="adf", profile=profile)
    finally:
        q.stop()
    assert res.ok and [d.pages for d in res.documents] == [1, 2]
    assert profile.processing.remove_blank_pages is True  # the profile is not changed


def test_separator_sheet_splits_a_batch():
    sheet = separators.separator_sheet("Invoices")
    page = reader(sheet).pages[0]
    assert [round(float(v)) for v in page.mediabox[2:]] == [612, 792]
    assert separators.read_barcodes(pages._render(sheet, 0, scale=150 / 72)) == ["OSH:Invoices"]
    batch = to_pdf(_page("A"))
    writer = PdfWriter()
    for part in (batch, sheet, batch):
        writer.add_page(reader(part).pages[0])
    buf = BytesIO()
    writer.write(buf)
    docs = separators.split_on_barcodes(buf.getvalue(), ProcessResult(pdf=buf.getvalue()))
    assert [r.barcode for _, r in docs] == ["", "Invoices"]
    fields = naming.collect_fields(docs[1][1], {"source": "adf"})
    assert naming.render("{barcode}", fields) == "Invoices.pdf"


def test_separator_sheet_route(live_server):
    r = live_server.get("/api/separator-sheet?label=Tax%20Papers", token=TOKEN)
    assert r.status == 200 and r.headers["Content-Type"] == "application/pdf"
    assert 'filename="separator_Tax_Papers.pdf"' in r.headers["Content-Disposition"]
    assert separators.read_barcodes(pages._render(r.body, 0, scale=150 / 72)) == \
        ["OSH:Tax Papers"]
    assert live_server.get("/api/separator-sheet", token=TOKEN).status == 400
    assert live_server.get("/api/separator-sheet?label=" + "x" * 81, token=TOKEN).status == 400
    assert live_server.get("/api/separator-sheet?label=A").status == 401
