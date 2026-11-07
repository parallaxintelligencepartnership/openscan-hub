"""Stage registry and the pipeline runner."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable

from .options import ProcessOptions, ProcessResult

logger = logging.getLogger(__name__)

# fn(pdf, options, result) -> bytes. `result` holds the work so far: a stage
# may append to result.warnings, fill result.page_texts or add to
# result.pages_removed. It returns the new PDF bytes.
StageFn = Callable[[bytes, ProcessOptions, ProcessResult], bytes]


class StageUnavailable(Exception):
    """Raise from a stage when an optional dependency is missing.

    The message is shown to the user as a warning, so make it plain:
    "Text recognition is not installed, so the scan was saved without it."
    """


@dataclass
class Stage:
    name: str
    order: int
    fn: StageFn
    enabled: Callable[[ProcessOptions], bool]


_stages: dict[str, Stage] = {}


def register_stage(name: str, order: int, fn: StageFn,
                   enabled: Callable[[ProcessOptions], bool]) -> None:
    """Add or replace a stage. Lower `order` runs first."""
    _stages[name] = Stage(name, order, fn, enabled)


def stages() -> list[Stage]:
    return sorted(_stages.values(), key=lambda s: (s.order, s.name))


def enabled_stages(options: ProcessOptions) -> list[Stage]:
    out = []
    for stage in stages():
        try:
            if stage.enabled(options):
                out.append(stage)
        except Exception as e:
            logger.warning("Could not check whether stage %s is on: %s", stage.name, e)
    return out


def run(pdf: bytes, options: ProcessOptions | None = None) -> ProcessResult:
    """Run every enabled stage in order and return the result.

    A stage that raises adds a warning and the scan goes on with the bytes
    from before that stage. With no stage enabled the bytes come back as is.
    """
    options = options or ProcessOptions()
    result = ProcessResult(pdf=pdf, options=options)
    for stage in enabled_stages(options):
        before_removed = result.pages_removed
        try:
            out = stage.fn(result.pdf, options, result)
        except StageUnavailable as e:
            logger.warning("Stage %s skipped: %s", stage.name, e)
            result.pages_removed = before_removed
            result.warnings.append(str(e))
            continue
        except Exception as e:
            logger.exception("Stage %s failed", stage.name)
            result.pages_removed = before_removed
            result.warnings.append(f"The {stage.name.replace('_', ' ')} step failed, so it was "
                                   f"skipped: {e}")
            continue
        if isinstance(out, (bytes, bytearray)) and out:
            result.pdf = bytes(out)
        else:
            result.warnings.append(f"The {stage.name.replace('_', ' ')} step returned nothing, "
                                   "so it was skipped.")
    return result
