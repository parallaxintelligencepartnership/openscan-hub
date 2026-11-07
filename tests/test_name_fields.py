"""Smart name fields: dates, titles, vendors, pass-through fields, fallbacks,
and two scans with the same name both landing."""

from datetime import date, datetime
from types import SimpleNamespace

import pytest

from openscan import intake, name_fields, naming
from openscan.config import AppConfig
from openscan.processing import ProcessResult
from openscan.profiles import Profile

NOW = datetime(2026, 3, 4, 5, 6, 7)
INVOICE = """ACME Corp
123 Main Street
Springfield

INVOICE
Invoice date: March 3, 2026
Due 04/02/2026
Total 42.00
"""


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setattr(naming, "_providers",
                        [name_fields.text_fields, name_fields.passthrough_fields])


def _name(rule, texts, config=None, **context):
    result = ProcessResult(pdf=b"", page_texts=list(texts))
    ctx = {"config": config or AppConfig(), "profile": Profile(name="P", name_rule=rule),
           "source": "adf", "now": NOW, "n": 1, **context}
    return naming.render(rule, naming.collect_fields(result, ctx))


@pytest.mark.parametrize("text, expected", [
    ("Date: 2026-03-04", date(2026, 3, 4)),
    ("Date: 2026/3/4", date(2026, 3, 4)),
    ("Date: 03/04/2026", date(2026, 3, 4)),  # US order: month first
    ("Date: 3-4-26", date(2026, 3, 4)),
    ("Issued March 3, 2026", date(2026, 3, 3)),
    ("Issued Mar. 3rd 2026", date(2026, 3, 3)),
    ("Issued 3 March 2026", date(2026, 3, 3)),
    ("Issued 21st of Sept. 2025", date(2025, 9, 21)),
    ("Bad 13/45/2026, good 1/2/2026", date(2026, 1, 2)),
    ("No date, only 12345 and 42.00", None),
])
def test_find_date(text, expected):
    assert name_fields.find_date(text) == expected


def test_first_date_by_position_wins():
    assert name_fields.find_date("Paid June 1, 2026 for order of 2026-01-15") == date(2026, 6, 1)


def test_smart_fields_from_text(providers):
    cfg = AppConfig()
    rule = "{vendor}_{doc_date}_{title}_{pages}p"
    assert _name(rule, [INVOICE], cfg) == "ACME Corp_2026-03-03_INVOICE_1p.pdf"
    cfg.naming.vendors = ["Springfield Power", "acme corp"]
    # A listed vendor wins, as the user wrote it, matched in any case.
    assert _name("{vendor}", [INVOICE], cfg) == "acme corp.pdf"
    assert _name("{doc_date:%Y-%m}", ["", INVOICE]) == "2026-03.pdf"


def test_title_prefers_a_heading_line(providers):
    text = "account no 1234-5678\nQuarterly Statement Summary\nsome body text here that is long"
    assert _name("{title}", [text]) == "Quarterly Statement Summary.pdf"


def test_missing_fields_use_rule_text_or_unknown(providers):
    assert _name("{vendor|no-vendor}_{doc_date|undated}_{title}", []) == \
        "no-vendor_undated_unknown.pdf"
    assert _name("{doc_date:%Y|undated}", ["no date here"]) == "undated.pdf"
    assert _name("{barcode|none}", ["text"]) == "none.pdf"


def test_pages_and_barcode_pass_through(providers):
    assert _name("{pages}_{barcode}", ["a", "b"], pages=7, barcode="INV-42") == "7_INV-42.pdf"
    assert _name("{pages}", ["a", "b", "c"]) == "3.pdf"


def test_fallback_text_does_not_change_old_rules():
    assert naming.render("{nothing}_{n}", {"n": 2}) == "unknown_002.pdf"
    assert naming.render("{n|x}", {"n": 5}) == "005.pdf"


def test_providers_only_parse_fields_the_rule_uses(monkeypatch):
    calls = []
    monkeypatch.setattr(name_fields, "find_title", lambda texts: calls.append(1) or "T")
    result = SimpleNamespace(page_texts=["x"], pdf=b"")
    ctx = {"profile": Profile(name_rule="scan_{n}")}
    assert "title" not in name_fields.text_fields(result, ctx)
    assert calls == []


def test_two_scans_with_the_same_name_both_land(tmp_path, config_dir, monkeypatch):
    monkeypatch.setattr(naming, "_providers", [])
    cfg = AppConfig()
    folder = tmp_path / "out"
    profile = Profile(id="p", name="P", name_rule="same-name",
                      destinations=[{"type": "folder", "options": {"folder": str(folder)}}])
    first = intake.handle_scan(cfg, b"%PDF-1.4 one", source="adf", profile=profile)
    second = intake.handle_scan(cfg, b"%PDF-1.4 two", source="adf", profile=profile)
    assert first.ok and second.ok
    assert first.filename == "same-name.pdf" and second.filename != first.filename
    assert sorted(p.read_bytes() for p in folder.iterdir()) == [b"%PDF-1.4 one", b"%PDF-1.4 two"]
