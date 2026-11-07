"""Processing pipeline: what happens to a scan before it is named and delivered.

Each stage is one module in this package that calls `register_stage` when
imported. To add a stage, write the module and add its name to `_STAGES`.
A module that fails to import (a missing optional dependency) is skipped
with a log line; the stage simply does not exist in that install.

Suggested `order` values, lower runs first:
    10 auto_rotate, 15 deskew, 20 crop, 30 remove_blank_pages,
    40 output_color, 50 ocr, 60 compression, 70 pdfa
"""

from __future__ import annotations

import importlib
import logging

from .options import COMPRESSION_CHOICES, OUTPUT_COLOR_CHOICES, ProcessOptions, ProcessResult
from .registry import StageUnavailable, enabled_stages, register_stage, run, stages

__all__ = [
    "COMPRESSION_CHOICES", "OUTPUT_COLOR_CHOICES", "ProcessOptions", "ProcessResult",
    "StageUnavailable", "enabled_stages", "register_stage", "run", "stages",
]

logger = logging.getLogger(__name__)

# Stage modules, imported once. Add one name per new stage module.
_STAGES = (
    "blank_pages",
)

for _name in _STAGES:
    try:
        importlib.import_module(f"{__name__}.{_name}")
    except ImportError as _e:  # pragma: no cover - depends on the install
        logger.info("Processing stage %s is not available: %s", _name, _e)
