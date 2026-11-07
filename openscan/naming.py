"""File names from a name rule such as "scan_{date}_{time}_{n}".

Fields: {date} {time} {n} {profile} {source}, plus whatever field providers
add (for example {doc_date}, {title}, {vendor} from text recognition).
A date or datetime value takes a strftime format: {date:%Y-%m}.
A missing or empty field becomes "unknown", or the rule's own text
after "|": {vendor|no-vendor}. The result is safe as a file
name on Windows and Linux and at most MAX_LENGTH characters long.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_LENGTH = 120
EXTENSION = ".pdf"
FALLBACK = "unknown"
DEFAULT_RULE = "scan_{date}_{time}_{n}"
BASE_FIELDS = ("date", "time", "n", "profile", "source")

# {field}, {field:format}, {field|fallback text}, {field:format|fallback text}
_FIELD_RE = re.compile(r"\{(\w+)(?::([^{}|]*))?(?:\|([^{}]*))?\}")
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}

# fn(process_result, context) -> {field: value}
FieldProvider = Callable[[Any, dict], dict]
_providers: list[FieldProvider] = []


def register_field_provider(fn: FieldProvider) -> FieldProvider:
    """Add a provider of extra name fields. Usable as a decorator.

    It gets the ProcessResult of the document and the context dict (keys:
    config, profile, source, started_by, now) and returns new fields. It
    cannot replace the base fields. Errors are logged and ignored.
    """
    if fn not in _providers:
        _providers.append(fn)
    return fn


def base_fields(*, now: Optional[datetime] = None, n: int = 1, profile: str = "",
                source: str = "") -> dict:
    now = now or datetime.now()
    return {"date": now, "time": now, "n": n, "profile": profile, "source": source}


def collect_fields(result: Any, context: dict) -> dict:
    """Base fields from `context`, then every provider's fields."""
    profile = context.get("profile")
    fields = base_fields(now=context.get("now"), n=int(context.get("n", 1) or 1),
                         profile=getattr(profile, "name", profile or "") or "",
                         source=str(context.get("source") or ""))
    for provider in list(_providers):
        try:
            extra = provider(result, context) or {}
        except Exception as e:
            logger.warning("Name field provider %s failed: %s",
                           getattr(provider, "__name__", provider), e)
            continue
        for key, value in extra.items():
            if key not in BASE_FIELDS:
                fields[key] = value
    return fields


def _format(key: str, value: Any, spec: Optional[str]) -> str:
    if value is None or value == "":
        return FALLBACK
    if isinstance(value, (datetime, date)):
        if not spec:
            spec = "%H%M%S" if key == "time" else "%Y-%m-%d"
        try:
            return value.strftime(spec)
        except (ValueError, TypeError):
            return FALLBACK
    if key == "n" and isinstance(value, int) and not spec:
        return str(value).zfill(3)
    if spec:
        try:
            return format(value, spec)
        except (ValueError, TypeError):
            pass
    return str(value)


def sanitize(name: str, extension: str = EXTENSION, max_length: int = MAX_LENGTH) -> str:
    """Make `name` a safe file name that ends in `extension`."""
    if extension and name.lower().endswith(extension.lower()):
        name = name[: -len(extension)]
    stem = _BAD_CHARS.sub("_", name)
    stem = stem.strip().lstrip(".").rstrip(". ")
    if not stem:
        stem = "scan"
    if stem.split(".")[0].upper() in _RESERVED:
        stem = "_" + stem
    room = max(1, max_length - len(extension))
    stem = stem[:room].rstrip(". ") or "scan"
    return stem + extension


def render(template: str, fields: Optional[dict] = None, extension: str = EXTENSION) -> str:
    """Fill `template` from `fields` and return a safe file name.

    Missing base fields get today's values (date, time), 1 (n) or the
    fallback text; any other missing or empty field is the text after "|"
    in the rule, else "unknown".
    """
    values = base_fields()
    values.update(fields or {})

    def replace(m: re.Match) -> str:
        key, spec, fallback = m.group(1), m.group(2), m.group(3)
        value = values.get(key)
        if fallback is not None and (value is None or value == ""):
            return fallback
        return _format(key, value, spec)

    name = _FIELD_RE.sub(replace, template or DEFAULT_RULE)
    return sanitize(name, extension)
