"""Scan profiles: one button = scanner settings + processing + name + destinations.

A config written before 0.4 has no profiles. On load it gets one "Default"
profile with `follow_settings=True`: at scan time that profile takes its
scanner settings, blank-page option, name rule and destinations from the
old Settings sections, so the Settings page keeps working as before. A
profile editor sets `follow_settings=False` when the user edits the profile.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field, fields, replace
from typing import Any, Optional

from . import edition
from .naming import DEFAULT_RULE
from .processing.options import ProcessOptions

logger = logging.getLogger(__name__)

DEFAULT_ID = "default"
SOURCES = ("auto", "adf", "flatbed")
COLOR_MODES = ("color", "gray", "bw")
PAGE_SIZES = ("auto", "letter", "a4", "legal")
SPLIT_MODES = ("none", "blank", "barcode")


@dataclass
class Profile:
    id: str = ""
    name: str = ""
    icon: str = ""
    source: str = "auto"  # auto | adf | flatbed
    color_mode: str = "color"  # color | gray | bw
    resolution: int = 300
    duplex: bool = False
    page_size: str = "auto"  # auto | letter | a4 | legal
    processing: ProcessOptions = field(default_factory=ProcessOptions)
    # Each item is {"type": <destination type>, "options": {...}}. Empty or
    # missing option values fall back to the old Settings sections.
    destinations: list[dict] = field(default_factory=list)
    name_rule: str = DEFAULT_RULE
    split: str = "none"  # none | blank | barcode
    show_on_home: bool = True
    follow_settings: bool = False

    def to_dict(self) -> dict:
        data = {f.name: getattr(self, f.name) for f in fields(self)}
        data["processing"] = self.processing.to_dict()
        data["destinations"] = [{"type": d.get("type", ""), "options": dict(d.get("options") or {})}
                                for d in self.destinations]
        return data


_CHOICES = {"source": SOURCES, "color_mode": COLOR_MODES, "page_size": PAGE_SIZES,
            "split": SPLIT_MODES}


def new_id(name: str = "") -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:24]
    return f"{slug or 'profile'}-{uuid.uuid4().hex[:6]}"


def from_dict(raw: Any) -> Optional[Profile]:
    """Build a Profile from JSON, skipping bad values. None if `raw` is not a dict."""
    if not isinstance(raw, dict):
        return None
    p = Profile()
    for f in fields(Profile):
        if f.name not in raw or f.name in ("processing", "destinations"):
            continue
        value, default = raw[f.name], getattr(p, f.name)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        else:
            ok = isinstance(value, str)
        if ok and f.name in _CHOICES:
            ok = value in _CHOICES[f.name]
        if ok:
            setattr(p, f.name, value)
        else:
            logger.warning("Ignoring bad profile value %s=%r", f.name, value)
    p.processing = ProcessOptions.from_dict(raw.get("processing"))
    dests = raw.get("destinations")
    if isinstance(dests, list):
        p.destinations = [{"type": d["type"], "options": dict(d.get("options") or {})}
                          for d in dests
                          if isinstance(d, dict) and isinstance(d.get("type"), str)
                          and isinstance(d.get("options", {}), dict)]
    if not p.id:
        p.id = new_id(p.name)
    if not p.name:
        p.name = p.id
    return p


def list_from_json(raw: Any) -> list[Profile]:
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        p = from_dict(item)
        if p is None or p.id in seen:
            continue
        seen.add(p.id)
        out.append(p)
    return out


_skipped_logged: set[str] = set()


def _log_skipped_once(kind: str) -> None:
    if kind not in _skipped_logged:
        _skipped_logged.add(kind)
        logger.info("This edition has no %s module; its settings are kept but not used", kind)


def destinations_from_settings(config) -> list[dict]:
    """The destinations the old Settings sections describe, in the old order."""
    dests: list[dict] = []
    if config.output.folder:
        dests.append({"type": "folder", "options": {"folder": config.output.folder}})
    p = config.paperless
    if p.enabled:
        if not edition.has("paperless"):
            _log_skipped_once("Paperless-NGX")
        elif p.mode == "consume":
            if p.consume_folder:
                dests.append({"type": "paperless_consume", "options": {"folder": p.consume_folder}})
        elif p.mode == "api":
            if p.api_url and p.api_token:
                # The token is not copied: an empty option means "use Settings".
                dests.append({"type": "paperless_api", "options": {
                    "url": p.api_url, "tags": list(p.tags), "correspondent": p.correspondent,
                    "document_type": p.document_type, "keep_local_copy": p.keep_local_copy}})
    return dests


def from_settings(config, *, id: str = DEFAULT_ID, name: str = "Default") -> Profile:
    """A profile that does what the old Settings sections say."""
    s = config.scan
    return Profile(
        id=id, name=name, source="auto", color_mode=s.color_mode, resolution=s.resolution,
        duplex=s.duplex, page_size=s.page_size,
        processing=ProcessOptions(remove_blank_pages=s.remove_blank_pages),
        destinations=destinations_from_settings(config),
        name_rule=config.output.filename_pattern or DEFAULT_RULE,
        follow_settings=True,
    )


def migrate(config) -> bool:
    """Give a config without profiles its "Default" profile. True if it changed."""
    if config.profiles:
        if not config.default_profile_id or not any(p.id == config.default_profile_id
                                                    for p in config.profiles):
            config.default_profile_id = config.profiles[0].id
            return True
        return False
    config.profiles = [from_settings(config)]
    config.default_profile_id = DEFAULT_ID
    return True


def get(config, profile_id: Optional[str] = None) -> Profile:
    """The profile to scan with: `profile_id`, else the default profile.

    A profile with follow_settings is rebuilt from the current Settings
    sections (keeping its id, name, icon, split and show_on_home).
    """
    found = None
    for wanted in (profile_id, getattr(config, "default_profile_id", "")):
        if wanted:
            found = next((p for p in config.profiles if p.id == wanted), None)
            if found:
                break
    if found is None and config.profiles and not profile_id:
        found = config.profiles[0]
    if found is None:
        if profile_id:
            logger.warning("Profile %r not found; using the default profile", profile_id)
            return get(config)
        return from_settings(config)
    return live(config, found)


def live(config, profile: Profile) -> Profile:
    """`profile` as it applies now: a follow_settings profile is rebuilt
    from the current Settings sections, keeping id, name, icon, split and
    show_on_home. Any other profile is returned as is."""
    if not profile.follow_settings:
        return profile
    current = from_settings(config, id=profile.id, name=profile.name)
    return replace(current, icon=profile.icon, split=profile.split,
                   show_on_home=profile.show_on_home)
