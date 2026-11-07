"""Build edition: "homelab" or "office". See docs/editions.md.

The edition is fixed when the package is built. It is not a setting.
Code reaches optional modules through `has(feature)` and a lazy import.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

HOMELAB = "homelab"
OFFICE = "office"
EDITIONS = (HOMELAB, OFFICE)

# feature -> editions that have it. "scan_role" is optional in Homelab and
# always on in Office; has() says whether the edition offers it.
FEATURES: dict[str, frozenset] = {
    "homeassistant": frozenset({HOMELAB}),
    "paperless": frozenset({HOMELAB}),
    "audit_default_on": frozenset({OFFICE}),
    "scan_role": frozenset({HOMELAB, OFFICE}),
}

PRODUCT_NAMES = {HOMELAB: "OpenScanHub", OFFICE: "OpenScanHub Office"}


def current() -> str:
    """The edition this package was built as. Unknown values mean homelab."""
    try:
        from ._edition import EDITION
    except ImportError:
        return HOMELAB
    return EDITION if EDITION in EDITIONS else HOMELAB


def has(feature: str) -> bool:
    """True when this edition offers `feature`. Unknown features are False."""
    return current() in FEATURES.get(feature, frozenset())


def features() -> dict[str, bool]:
    return {name: has(name) for name in FEATURES}


def product_name() -> str:
    return PRODUCT_NAMES[current()]


def edition_file() -> Path:
    return Path(__file__).with_name("_edition.py")


def set_edition(edition: str, path: Path | None = None) -> Path:
    """Rewrite _edition.py. Used by builds before packaging."""
    if edition not in EDITIONS:
        raise ValueError(f"edition must be one of {', '.join(EDITIONS)}")
    target = path or edition_file()
    target.write_text(
        "# Written by `python -m openscan.edition --set office|homelab`. Do not edit by hand.\n"
        f'EDITION = "{edition}"\n',
        encoding="utf-8",
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m openscan.edition",
                                     description="Show or set the OpenScanHub build edition.")
    parser.add_argument("--set", choices=EDITIONS, help="write this edition into _edition.py")
    args = parser.parse_args(argv)
    if args.set:
        path = set_edition(args.set)
        print(f"Edition set to {args.set} in {path}")
    else:
        print(current())
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
