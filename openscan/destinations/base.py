"""Destination base class and the shared delivery errors."""

from __future__ import annotations

from typing import Any, ClassVar, Optional

FIELD_KINDS = ("text", "password", "path", "bool", "choice", "list")


class DeliveryError(Exception):
    """A destination failed; the message is meant for the user."""


def field(key: str, label: str, kind: str = "text", *, required: bool = False,
          default: Any = None, choices: Optional[list] = None) -> dict:
    """One entry of Destination.FIELDS, the form the settings UI draws."""
    if kind not in FIELD_KINDS:
        raise ValueError(f"unknown field kind {kind!r}")
    if default is None:
        default = {"bool": False, "list": []}.get(kind, "")
    return {"key": key, "label": label, "kind": kind, "required": required,
            "default": default, "choices": list(choices or [])}


class Destination:
    """One place a finished document can go.

    Subclasses set the class attributes and implement deliver() and test().
    `options` is the dict stored in a profile ({type, options}); keys are the
    FIELDS keys. Destinations hold no state between calls.
    """

    type: ClassVar[str] = ""
    label: ClassVar[str] = ""
    feature: ClassVar[Optional[str]] = None  # edition feature, None = every edition
    FIELDS: ClassVar[list[dict]] = []
    # Key in options that names where the file goes (shown in the retry queue).
    target_key: ClassVar[str] = ""

    def deliver(self, pdf: bytes, filename: str, options: dict) -> tuple[str, bool]:
        """Deliver one document. Returns (location, confirmed).

        `location` is a file path or URL for the user. `confirmed` is False
        when the destination accepted the file but has not finished with it
        yet. Raises DeliveryError with a plain message on failure.
        """
        raise NotImplementedError

    def test(self, options: dict) -> dict:
        """Check the settings without delivering a scan. Returns {ok, message}."""
        return {"ok": True, "message": "Nothing to test."}

    # --- helpers used by the delivery loop and the retry queue -------------

    @classmethod
    def defaults(cls) -> dict:
        return {f["key"]: f["default"] for f in cls.FIELDS}

    @classmethod
    def with_defaults(cls, options: Optional[dict]) -> dict:
        merged = cls.defaults()
        merged.update(options or {})
        return merged

    @classmethod
    def secret_keys(cls) -> set[str]:
        return {f["key"] for f in cls.FIELDS if f["kind"] == "password"}

    @classmethod
    def target(cls, options: dict) -> str:
        return str((options or {}).get(cls.target_key, "")) if cls.target_key else ""

    @classmethod
    def public_options(cls, options: dict) -> dict:
        """`options` without secrets, safe to write to the retry queue file."""
        secret = cls.secret_keys()
        return {k: v for k, v in (options or {}).items() if k not in secret}

    @classmethod
    def describe(cls) -> dict:
        """What the settings UI needs to draw a form for this destination."""
        return {"type": cls.type, "label": cls.label, "fields": [dict(f) for f in cls.FIELDS]}
