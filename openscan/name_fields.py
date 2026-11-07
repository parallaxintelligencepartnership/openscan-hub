"""Smart name fields read from the scanned text.

    {doc_date}  first date in the text (US, ISO or written), as a date
    {title}     first line that looks like a heading
    {vendor}    a name from config.naming.vendors found in the text, else
                the most prominent line on page 1
    {pages}     page count (from the context, else counted)
    {barcode}   barcode text, when the context or the result has one

The fields need text recognition (ProcessResult.page_texts). Without text
they are missing, and the name rule shows its fallback text or "unknown".
A provider only does work for the fields the profile's name rule uses.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable, Optional

from .naming import register_field_provider

MAX_FIELD_LENGTH = 40
_LINES_TO_SCAN = 20

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH_RE = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|"
             r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")

# Each pattern gives named groups y, m (number or month name) and d.
_DATE_PATTERNS = [
    # 2026-03-04, 2026/3/4, 2026.03.04
    re.compile(r"(?<!\d)(?P<y>(?:19|20)\d{2})[-/.](?P<m>\d{1,2})[-/.](?P<d>\d{1,2})(?!\d)"),
    # 03/04/2026, 3-4-26 (US order: month first)
    re.compile(r"(?<!\d)(?P<m>\d{1,2})[/\-.](?P<d>\d{1,2})[/\-.](?P<y>(?:19|20)?\d{2})(?!\d)"),
    # March 3, 2026 / Mar. 3rd 2026
    re.compile(_MONTH_RE.replace("(", "(?P<m>", 1)
               + r"\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>(?:19|20)\d{2})(?!\d)",
               re.IGNORECASE),
    # 3 March 2026 / 3rd of March, 2026
    re.compile(r"(?<!\d)(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?"
               + _MONTH_RE.replace("(", "(?P<m>", 1)
               + r",?\s+(?P<y>(?:19|20)\d{2})(?!\d)", re.IGNORECASE),
]


def _to_date(y: str, m: str, d: str) -> Optional[date]:
    try:
        year = int(y)
        if year < 100:
            year += 2000 if year < 70 else 1900
        month = int(m) if m.isdigit() else _MONTHS.get(m.lower()[:3])
        if not month:
            return None
        return date(year, month, int(d))
    except (TypeError, ValueError):
        return None


def find_date(text: str) -> Optional[date]:
    """The first valid date in `text` (by position), or None."""
    best: Optional[tuple[int, date]] = None
    for pattern in _DATE_PATTERNS:
        for m in pattern.finditer(text or ""):
            if best is not None and m.start() >= best[0]:
                break
            found = _to_date(m.group("y"), m.group("m"), m.group("d"))
            if found is not None:
                best = (m.start(), found)
                break
    return best[1] if best else None


def _lines(text: str) -> list[str]:
    out = []
    for raw in (text or "").splitlines():
        line = " ".join(raw.split())
        if line:
            out.append(line)
        if len(out) >= _LINES_TO_SCAN:
            break
    return out


def _wordy(line: str) -> bool:
    """A line that reads like a name or heading, not a number or a date."""
    letters = sum(c.isalpha() for c in line)
    if letters < 2 or len(line) > 80:
        return False
    if letters / max(1, len(line.replace(" ", ""))) < 0.6:
        return False
    return find_date(line) is None


def _is_heading(line: str) -> bool:
    words = line.split()
    if not 1 <= len(words) <= 8:
        return False
    if line.isupper():
        return True
    caps = [w for w in words if w[:1].isalpha()]
    return bool(caps) and all(w[:1].isupper() for w in caps)


def _shorten(value: str) -> str:
    value = value.strip(" .,:;-_")
    if len(value) <= MAX_FIELD_LENGTH:
        return value
    return value[:MAX_FIELD_LENGTH].rsplit(" ", 1)[0] or value[:MAX_FIELD_LENGTH]


def find_title(texts: Iterable[str]) -> str:
    """First heading-like line: an all-caps short line wins, then a Title Case line."""
    for text in texts:
        lines = [line for line in _lines(text) if _wordy(line)]
        if not lines:
            continue
        for test in (lambda line: line.isupper() and len(line.split()) <= 6, _is_heading):
            for line in lines:
                if test(line):
                    return _shorten(line)
        return _shorten(lines[0])
    return ""


def find_vendor(texts: list[str], vendors: Iterable[str]) -> str:
    """A known vendor found in the text (earliest on the earliest page), else
    the most prominent line on page 1: the first short line of words, where
    a letterhead usually is."""
    names = [v.strip() for v in vendors or [] if isinstance(v, str) and v.strip()]
    for text in texts:
        best: Optional[tuple[int, str]] = None
        for name in names:
            m = re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text or "", re.IGNORECASE)
            if m and (best is None or m.start() < best[0]):
                best = (m.start(), name)
        if best:
            return best[1]
    first = next((t for t in texts if (t or "").strip()), "")
    lines = [line for line in _lines(first)[:8] if _wordy(line) and len(line.split()) <= 6]
    return _shorten(lines[0]) if lines else ""


def _wants(context: dict, key: str) -> bool:
    rule = getattr(context.get("profile"), "name_rule", None)
    return rule is None or ("{" + key) in rule


def _texts(result: Any) -> list[str]:
    return [t for t in (getattr(result, "page_texts", None) or []) if isinstance(t, str)]


@register_field_provider
def text_fields(result: Any, context: dict) -> dict:
    texts = _texts(result)
    out: dict = {}
    if not any(t.strip() for t in texts):
        return out
    if _wants(context, "doc_date"):
        for text in texts:
            found = find_date(text)
            if found:
                out["doc_date"] = found
                break
    if _wants(context, "title"):
        out["title"] = find_title(texts)
    if _wants(context, "vendor"):
        config = context.get("config")
        vendors = getattr(getattr(config, "naming", None), "vendors", None) or []
        out["vendor"] = find_vendor(texts, vendors)
    return out


@register_field_provider
def passthrough_fields(result: Any, context: dict) -> dict:
    out: dict = {}
    if _wants(context, "pages"):
        pages = context.get("pages")
        if pages is None:
            pages = _count_pages(result)
        if pages:
            out["pages"] = pages
    if _wants(context, "barcode"):
        barcode = context.get("barcode") or getattr(result, "barcode", None)
        if not barcode:
            codes = getattr(result, "barcodes", None)
            barcode = codes[0] if isinstance(codes, (list, tuple)) and codes else None
        if barcode:
            out["barcode"] = str(barcode)
    return out


def _count_pages(result: Any) -> int:
    pdf = getattr(result, "pdf", None)
    if pdf:
        try:
            from .pages import page_count
            return page_count(pdf)
        except Exception:
            pass
    return len(_texts(result))
