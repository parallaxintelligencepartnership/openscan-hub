"""Destinations: where finished documents go.

A destination is a `Destination` subclass in its own module in this package.
To add one, write the module, decorate the class with `@register`, and add
the module name to `_MODULES` below. See docs/architecture.md.

`deliver_all` sends one document to a list of {type, options} targets and
hands every failure to the retry queue; it never throws a scan away.
"""

from __future__ import annotations

import importlib
import logging
import os
import re
import threading
from dataclasses import asdict, dataclass, field
from typing import Optional

from .. import edition
from .base import DeliveryError, Destination, FIELD_KINDS, field as option_field

__all__ = [
    "DeliveryError", "DeliveryResult", "Destination", "FIELD_KINDS", "option_field",
    "register", "get", "available", "all_types", "deliver_one", "deliver_all",
    "resolve_options", "secret_option_keys", "blank_secrets",
]

logger = logging.getLogger(__name__)

# Built-in destination modules. A module that is missing from the build (an
# Office build deletes paperless.py) is skipped.
_MODULES = ("folder", "paperless", "smb", "webdav", "email")

_registry: dict[str, type[Destination]] = {}
_loaded = False
_load_lock = threading.Lock()


def register(cls: type[Destination]) -> type[Destination]:
    """Add a Destination class to the registry. Usable as a decorator."""
    if not cls.type:
        raise ValueError(f"{cls.__name__} has no type")
    _registry[cls.type] = cls
    return cls


def _load() -> None:
    global _loaded
    if _loaded:
        return
    with _load_lock:
        if _loaded:
            return
        for name in _MODULES:
            try:
                module = importlib.import_module(f"{__name__}.{name}")
            except ImportError as e:
                logger.debug("Destination module %s is not in this build: %s", name, e)
                continue
            for obj in vars(module).values():
                if (isinstance(obj, type) and issubclass(obj, Destination)
                        and obj is not Destination and obj.type and obj.type not in _registry):
                    register(obj)
        _loaded = True


def _allowed(cls: type[Destination]) -> bool:
    return cls.feature is None or edition.has(cls.feature)


def get(type_: str) -> Optional[type[Destination]]:
    """The class for `type_`, or None if it is unknown or not in this edition."""
    _load()
    cls = _registry.get(type_)
    return cls if cls is not None and _allowed(cls) else None


def available() -> list[type[Destination]]:
    """Destination classes this edition offers, in registration order."""
    _load()
    return [cls for cls in _registry.values() if _allowed(cls)]


def all_types() -> list[str]:
    """Every registered type, including those this edition hides."""
    _load()
    return list(_registry)


# --- secrets ---------------------------------------------------------------

# Secret option keys of destination types that a build may not ship (an
# Office build has no paperless.py). A config made by another edition still
# holds their secrets, so they must be blanked without the class.
KNOWN_SECRET_KEYS: dict[str, frozenset] = {"paperless_api": frozenset({"token"})}
_SECRET_WORD = re.compile(r"password|token|secret|key", re.IGNORECASE)


def secret_option_keys(type_: str, options: Optional[dict] = None) -> set[str]:
    """Option keys of `type_` that hold a secret.

    A type in this build: its password fields. A type not in this build:
    every key whose name has password, token, secret or key in it. Both get
    the static KNOWN_SECRET_KEYS too.
    """
    keys = set(KNOWN_SECRET_KEYS.get(type_, ()))
    cls = get(type_)
    if cls is not None:
        keys |= cls.secret_keys()
    else:
        keys |= {k for k in (options or {})
                 if isinstance(k, str) and not k.startswith("has_") and _SECRET_WORD.search(k)}
    return keys


def blank_secrets(type_: str, options: dict) -> dict:
    """Blank the secrets in `options` in place and add has_<key> flags."""
    for key in sorted(secret_option_keys(type_, options)):
        options[f"has_{key}"] = bool(options.get(key))
        options[key] = ""
    return options


# --- options ---------------------------------------------------------------

# Types whose empty options fall back to the old Settings sections.
SETTINGS_FALLBACK_TYPES = ("folder", "paperless_consume", "paperless_api")


def _legacy_options(type_: str, config) -> dict:
    """Options for `type_` taken from the pre-0.4 settings sections."""
    if config is None:
        return {}
    p = config.paperless
    if type_ == "folder":
        return {"folder": config.output.folder}
    if type_ == "paperless_consume":
        return {"folder": p.consume_folder}
    if type_ == "paperless_api":
        return {"url": p.api_url, "token": p.api_token, "tags": list(p.tags),
                "correspondent": p.correspondent, "document_type": p.document_type,
                "keep_local_copy": p.keep_local_copy}
    return {}


def resolve_options(type_: str, options: Optional[dict], config=None) -> dict:
    """Full options for one delivery.

    Missing or empty values fall back to the old Settings sections (a profile
    that says {"type": "folder", "options": {}} uses the scans folder from
    Settings), then to the FIELDS defaults.
    """
    cls = get(type_)
    merged = cls.defaults() if cls else {}
    merged.update({k: v for k, v in _legacy_options(type_, config).items() if v not in ("", None)})
    merged.update({k: v for k, v in (options or {}).items() if v not in ("", None)})
    return merged


def secrets_for(type_: str, target: str, config) -> dict:
    """Secrets for a queued delivery. The queue file never holds them, so a
    retry finds them again in the profiles, then in the old sections."""
    cls = get(type_)
    if cls is None or config is None or not cls.secret_keys():
        return {}
    keys = cls.secret_keys()
    for profile in getattr(config, "profiles", None) or []:
        for dest in getattr(profile, "destinations", None) or []:
            if not isinstance(dest, dict) or dest.get("type") != type_:
                continue
            opts = resolve_options(type_, dest.get("options"), config)
            if cls.target(opts) == target:
                found = {k: opts[k] for k in keys if opts.get(k)}
                if found:
                    return found
    legacy = _legacy_options(type_, config)
    return {k: legacy[k] for k in keys if legacy.get(k)}


# --- delivery --------------------------------------------------------------

@dataclass
class DeliveryResult:
    """Outcome of delivering one document to every destination."""

    ok: bool = False
    saved_paths: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    queued: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def deliver_one(type_: str, pdf: bytes, filename: str, options: dict) -> tuple[str, bool]:
    """Deliver through the registry. Returns (location, confirmed).

    Raises DeliveryError, also when the type is not in this edition.
    """
    cls = get(type_)
    if cls is None:
        raise DeliveryError(f"The destination {type_!r} is not available in this edition.")
    return cls().deliver(pdf, filename, options)


def deliver_all(pdf: bytes, filename: str, targets: list[dict], *,
                source_path: Optional[str] = None, source_owned: bool = False,
                queue_failures: bool = True) -> DeliveryResult:
    """Deliver `pdf` to every target ({"type", "options"}, options resolved).

    Every failed target goes to the retry queue. If `source_path` is given,
    the queue retries from that file (and deletes it once delivered when
    `source_owned`); otherwise the queue keeps its own copy of the bytes.

    A confirmed target with keep_local_copy=False removes the copy a folder
    target saved, only when every target succeeded.
    """
    result = DeliveryResult()
    if not targets:
        result.errors.append("No destination is set up. Choose a scans folder in Settings.")
        return result

    failures: list[tuple[str, str, dict]] = []
    local_paths: list[str] = []
    drop_local = False
    for target in targets:
        type_ = target.get("type", "")
        options = dict(target.get("options") or {})
        cls = get(type_)
        try:
            location, confirmed = deliver_one(type_, pdf, filename, options)
        except DeliveryError as e:
            result.errors.append(str(e))
            if cls is not None:
                failures.append((type_, cls.target(options), cls.public_options(options)))
            continue
        except Exception as e:
            # A bug or an unexpected library error in one destination must not
            # lose the scan or stop the next destination. The text of the
            # error can hold a secret, so only its class name is logged.
            label = cls.label if cls is not None else type_
            logger.error("Destination %s failed with an unexpected %s", type_, type(e).__name__)
            result.errors.append(f"{label}: the delivery failed with an unexpected error "
                                 f"({type(e).__name__}). OpenScanHub keeps the scan and tries again.")
            if cls is not None:
                failures.append((type_, cls.target(options), cls.public_options(options)))
            continue
        result.saved_paths.append(location)
        if type_ == "folder":
            local_paths.append(location)
        if confirmed and options.get("keep_local_copy") is False:
            drop_local = True

    if drop_local and local_paths and not failures:
        for path in local_paths:
            try:
                os.unlink(path)
                result.saved_paths.remove(path)
                logger.info("Removed local copy %s (the upload was confirmed)", path)
            except OSError as e:
                logger.warning("Could not remove local copy %s: %s", path, e)

    if failures and queue_failures:
        try:
            from ..retry_queue import get_queue
            get_queue().add(
                pdf_data=None if source_path else pdf,
                source_path=source_path,
                source_owned=source_owned,
                filename=filename,
                destinations=failures,
                error="; ".join(result.errors),
            )
            result.queued = True
        except Exception as e:
            logger.error("Could not queue %s for retry: %s", filename, e)

    result.ok = not failures and not result.errors
    return result
