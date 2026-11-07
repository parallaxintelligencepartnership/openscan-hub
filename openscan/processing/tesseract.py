"""Find and run the Tesseract program (Apache-2.0).

OpenScanHub calls Tesseract as a separate program; it never links it.
The Docker image installs it. On Windows the user installs it, and this
module looks on PATH and in the standard install folders.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

TIMEOUT = 180  # seconds for one page

MISSING = ("Text recognition needs the free Tesseract program, and it is not installed, "
           "so this step was skipped.")
if sys.platform == "win32":  # pragma: no cover - depends on the platform
    MISSING = ("Text recognition needs the free Tesseract program, and it was not found, so "
               "this step was skipped. Install Tesseract for Windows and scan again.")

_LANG_RE = re.compile(r"^[A-Za-z0-9_]+$")
_cache: dict[str, object] = {}


def _windows_candidates() -> list[Path]:
    out = []
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        base = os.environ.get(var)
        if base:
            out.append(Path(base) / "Tesseract-OCR" / "tesseract.exe")
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "Programs" / "Tesseract-OCR" / "tesseract.exe")
    return out


def find() -> Optional[str]:
    """The path of the tesseract program, or None."""
    found = shutil.which("tesseract")
    if found:
        return found
    if sys.platform == "win32":
        for candidate in _windows_candidates():
            if candidate.is_file():
                return str(candidate)
    return None


def _creationflags() -> int:
    # The windowed EXE must not flash a console window for each page.
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0


def run(args: list[str], timeout: int = TIMEOUT) -> subprocess.CompletedProcess:
    """Run tesseract with `args`. Raises FileNotFoundError when it is missing."""
    exe = find()
    if not exe:
        raise FileNotFoundError("tesseract")
    env = dict(os.environ)
    # Pages run in parallel; one thread per Tesseract process is faster.
    env.setdefault("OMP_THREAD_LIMIT", "1")
    return subprocess.run([exe, *args], capture_output=True, timeout=timeout, env=env,
                          creationflags=_creationflags(), check=False)


def languages() -> set[str]:
    """Installed language data names, for example {"eng", "deu", "osd"}."""
    exe = find()
    if not exe:
        return set()
    key = f"langs:{exe}"
    if key not in _cache:
        try:
            proc = run(["--list-langs"], timeout=30)
            text = (proc.stdout or b"").decode("utf-8", "replace")
            # Tesseract 3 printed the list on stderr.
            text += (proc.stderr or b"").decode("utf-8", "replace")
            _cache[key] = {line.strip() for line in text.splitlines()[1:]
                           if _LANG_RE.match(line.strip())}
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("Could not list Tesseract languages: %s", e)
            return set()
    return set(_cache[key])  # type: ignore[arg-type]


def pick_languages(wanted: list[str]) -> tuple[str, list[str]]:
    """Return ("eng+deu", [missing names]) for the installed subset of `wanted`.

    The first string is empty when no wanted language is installed.
    """
    have = languages()
    use, missing = [], []
    for name in wanted or ["eng"]:
        name = str(name).strip()
        if not _LANG_RE.match(name):
            missing.append(name)
        elif name in have:
            if name not in use:
                use.append(name)
        else:
            missing.append(name)
    return "+".join(use), missing
