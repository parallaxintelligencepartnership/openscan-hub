"""Name rules: fields, formats, fallbacks, file-system safety, providers."""

from datetime import date, datetime

import pytest

from openscan import naming
from openscan.output import generate_filename

NOW = datetime(2026, 3, 4, 5, 6, 7)


@pytest.fixture
def providers(monkeypatch):
    monkeypatch.setattr(naming, "_providers", [])
    return naming._providers


def test_old_patterns_render_the_same(monkeypatch):
    import openscan.output as output

    class FixedDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW

    monkeypatch.setattr(output, "datetime", FixedDatetime)
    for pattern in ("scan_{date}_{time}_{n}", "scan_{n}", "{date}", "invoice-{date}_{time}",
                    "already.pdf", "scan_{date}_{time}_{n}.pdf"):
        assert naming.render(pattern, naming.base_fields(now=NOW)) == generate_filename(pattern)


def test_fields_and_formats():
    fields = naming.base_fields(now=NOW, n=12, profile="Invoices", source="adf")
    assert naming.render("{profile}_{source}_{date:%Y-%m}_{n}", fields) == "Invoices_adf_2026-03_012.pdf"
    assert naming.render("{time:%H-%M}", fields) == "05-06.pdf"
    assert naming.render("{doc_date:%d.%m.%Y}", {"doc_date": date(2025, 12, 1)}) == "01.12.2025.pdf"


def test_unknown_and_empty_fields_fall_back():
    assert naming.render("{vendor}_{profile}", {"profile": ""}) == "unknown_unknown.pdf"
    assert naming.render("{date:%Q-%%}", {"date": NOW}).endswith(".pdf")


@pytest.mark.parametrize("template, expected", [
    ("a/b\\c:d*e?f\"g<h>i|j", "a_b_c_d_e_f_g_h_i_j.pdf"),
    ("CON", "_CON.pdf"),
    ("lpt1.x", "_lpt1.x.pdf"),
    ("...hidden", "hidden.pdf"),
    ("name. . ", "name.pdf"),
    ("", None),
    ("tab\there", "tab_here.pdf"),
])
def test_names_are_safe(template, expected):
    name = naming.render(template, naming.base_fields(now=NOW))
    if expected:
        assert name == expected
    assert not any(c in name for c in '<>:"/\\|?*\t')
    assert len(name) <= naming.MAX_LENGTH


def test_long_names_are_cut_to_120():
    name = naming.render("{title}", {"title": "x" * 500})
    assert len(name) == 120 and name.endswith(".pdf")


def test_field_providers_add_but_never_replace(providers):
    @naming.register_field_provider
    def vendor(result, context):
        return {"vendor": "ACME / Inc", "date": "hijack"}

    def broken(result, context):
        raise RuntimeError("no text")

    naming.register_field_provider(broken)
    fields = naming.collect_fields(None, {"now": NOW, "profile": "Bills", "source": "ftp"})
    assert fields["vendor"] == "ACME / Inc" and fields["date"] == NOW
    assert naming.render("{vendor}_{profile}_{date}", fields) == "ACME _ Inc_Bills_2026-03-04.pdf"
