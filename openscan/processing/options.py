"""Processing options and result types."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, fields
from typing import Any

logger = logging.getLogger(__name__)

COMPRESSION_CHOICES = ("none", "standard", "small")
OUTPUT_COLOR_CHOICES = ("keep", "gray", "bw")


@dataclass
class ProcessOptions:
    """What to do to a scan between the scanner and the destinations.

    Every option is off by default, so a default profile leaves the bytes
    exactly as the scanner sent them.
    """

    ocr: bool = False
    ocr_languages: list[str] = field(default_factory=lambda: ["eng"])
    deskew: bool = False
    auto_rotate: bool = False
    remove_blank_pages: bool = False
    crop: bool = False
    compression: str = "none"  # none | standard | small
    output_color: str = "keep"  # keep | gray | bw
    pdfa: bool = False

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, raw: Any) -> "ProcessOptions":
        """Build from JSON, ignoring unknown keys and wrong types."""
        opts = cls()
        if not isinstance(raw, dict):
            return opts
        for f in fields(cls):
            if f.name not in raw:
                continue
            value, default = raw[f.name], getattr(opts, f.name)
            if isinstance(default, bool):
                ok = isinstance(value, bool)
            elif isinstance(default, list):
                ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
            else:
                ok = isinstance(value, str)
            if f.name == "compression":
                ok = ok and value in COMPRESSION_CHOICES
            if f.name == "output_color":
                ok = ok and value in OUTPUT_COLOR_CHOICES
            if ok:
                setattr(opts, f.name, list(value) if isinstance(value, list) else value)
            else:
                logger.warning("Ignoring bad processing option %s=%r", f.name, value)
        return opts


@dataclass
class ProcessResult:
    """A processed document.

    `page_texts` has one entry per page once an OCR stage has run (empty
    before). `warnings` are plain sentences for the user.
    """

    pdf: bytes
    page_texts: list[str] = field(default_factory=list)
    pages_removed: int = 0
    warnings: list[str] = field(default_factory=list)
