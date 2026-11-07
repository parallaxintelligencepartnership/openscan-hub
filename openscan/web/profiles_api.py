"""Scan profile endpoints: list, edit, order, default, one-tap scan, name
preview, destination forms and tests, and the vendor list for {vendor}.

    GET  /api/profiles                     {"profiles": [...], "default_profile_id"}
    POST /api/profiles                     create; body is a profile
    GET  /api/profiles/<id>                one profile
    POST /api/profiles/<id>                update (only the keys in the body)
    POST /api/profiles/<id>/delete
    POST /api/profiles/<id>/default        make it the default profile
    POST /api/profiles/<id>/scan           scan, process, name and deliver
    POST /api/profiles/reorder             {"ids": [...]}
    POST /api/profiles/preview             {"name_rule", "text"?} -> {"filename"}
    GET  /api/destinations                 destination types and their FIELDS
    POST /api/destinations/test            {"type", "options", "profile_id"?, "index"?}
    GET  /api/naming/vendors               {"vendors": [...]}
    POST /api/naming/vendors               {"vendors": [...]}

Secret options (FIELDS of kind password, and secret-looking keys of types
this edition does not ship) are blank in every answer, with "has_<key>"
telling whether one is stored. The scan role gets only id, name, icon and
show_on_home of each profile. A blank secret in an update keeps
the stored one. Any edit turns follow_settings off: from then on the profile
keeps its own values instead of following the Settings page.
"""

from __future__ import annotations

import copy
import logging
import re
import threading
from datetime import datetime
from typing import Any, Optional

from .. import destinations, naming, profiles
from ..processing import COMPRESSION_CHOICES, OUTPUT_COLOR_CHOICES, ProcessOptions, ProcessResult
from ..profiles import Profile
from .server import BadRequest, get_config, route

logger = logging.getLogger(__name__)

MAX_PROFILES = 50
MAX_DESTINATIONS = 10
MAX_NAME = 60
MAX_ICON = 16
MAX_RULE = 200
MAX_VENDORS = 500
RESOLUTION_RANGE = (75, 1200)
_LANGUAGE_RE = re.compile(r"^[A-Za-z_]{2,20}$")
SAMPLE_TEXT_LIMIT = 20000

# One edit at a time: read the live config, change a copy, commit it.
_edit_lock = threading.Lock()


# --- output -------------------------------------------------------------------

def public_profile(config, profile: Profile) -> dict:
    """The profile as it applies now, with secrets blanked (also for
    destination types this edition does not ship)."""
    data = profiles.live(config, profile).to_dict()
    for dest in data["destinations"]:
        if isinstance(dest.get("options"), dict):
            destinations.blank_secrets(str(dest.get("type", "")), dest["options"])
    data["is_default"] = profile.id == _default_id(config)
    return data


# The scan role only needs these to draw the profile buttons.
SCAN_ROLE_PROFILE_KEYS = ("id", "name", "icon", "show_on_home")


def _scan_view(config) -> dict:
    return {"profiles": [{k: getattr(p, k) for k in SCAN_ROLE_PROFILE_KEYS}
                         for p in _stored(config)],
            "default_profile_id": _default_id(config)}


def _stored(config) -> list[Profile]:
    return list(config.profiles) if config.profiles else [profiles.from_settings(config)]


def _default_id(config) -> str:
    stored = _stored(config)
    wanted = config.default_profile_id
    return wanted if any(p.id == wanted for p in stored) else stored[0].id


def _listing(config) -> dict:
    return {"profiles": [public_profile(config, p) for p in _stored(config)],
            "default_profile_id": _default_id(config)}


# --- validation ---------------------------------------------------------------

def _text(body: dict, key: str, label: str, limit: int, *, required: bool = False) -> str:
    value = body.get(key)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise BadRequest(f"{label} must be text")
    value = value.strip()
    if required and not value:
        raise BadRequest(f"{label} is required")
    if len(value) > limit:
        raise BadRequest(f"{label} must be {limit} characters or fewer")
    return value


def _bool(value: Any, label: str) -> bool:
    if not isinstance(value, bool):
        raise BadRequest(f"{label} must be on or off")
    return value


def _choice(value: Any, choices, label: str) -> str:
    if value not in choices:
        raise BadRequest(f"{label} has an unknown value")
    return value


def _str_list(value: Any, label: str) -> list[str]:
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise BadRequest(f"{label} must be a list of text")
    return [v.strip() for v in value if v.strip()]


def _processing(raw: Any) -> ProcessOptions:
    if not isinstance(raw, dict):
        raise BadRequest("Processing must be an object")
    opts = ProcessOptions()
    for key, value in raw.items():
        label = f"Processing option {key}"
        if key == "ocr_languages":
            langs = _str_list(value, "Text recognition languages")
            if not langs or not all(_LANGUAGE_RE.match(v) for v in langs):
                raise BadRequest("Text recognition languages must be codes like eng or deu")
            opts.ocr_languages = langs
        elif key == "compression":
            opts.compression = _choice(value, COMPRESSION_CHOICES, "Compression")
        elif key == "output_color":
            opts.output_color = _choice(value, OUTPUT_COLOR_CHOICES, "Output color")
        elif hasattr(opts, key) and isinstance(getattr(opts, key), bool):
            setattr(opts, key, _bool(value, label))
        # Unknown keys are ignored, as in the settings API.
    return opts


def _option(field: dict, value: Any) -> Any:
    label, kind = field["label"], field["kind"]
    if kind == "bool":
        return _bool(value, label)
    if kind == "list":
        return _str_list(value, label)
    if value is None:
        value = ""
    if not isinstance(value, str):
        raise BadRequest(f"{label} must be text")
    value = value.strip()
    if kind == "choice" and value and field["choices"] and value not in field["choices"]:
        raise BadRequest(f"{label} has an unknown value")
    if len(value) > 2000:
        raise BadRequest(f"{label} is too long")
    return value


def _find_old_secret(old: Optional[Profile], index: int, type_: str, key: str) -> str:
    """The stored secret for destination `index` (or the first of that type)."""
    if old is None:
        return ""
    dests = old.destinations
    candidates = ([dests[index]] if 0 <= index < len(dests) else []) + list(dests)
    for dest in candidates:
        if dest.get("type") == type_ and (dest.get("options") or {}).get(key):
            return dest["options"][key]
    return ""


def _destinations(raw: Any, old: Optional[Profile]) -> list[dict]:
    if not isinstance(raw, list):
        raise BadRequest("Destinations must be a list")
    if not raw:
        raise BadRequest("Add at least one destination")
    if len(raw) > MAX_DESTINATIONS:
        raise BadRequest(f"A profile can have at most {MAX_DESTINATIONS} destinations")
    out = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise BadRequest("Each destination needs a type")
        cls = destinations.get(item["type"])
        if cls is None:
            raise BadRequest(f"The destination {item['type']!r} is not available")
        options = item.get("options") or {}
        if not isinstance(options, dict):
            raise BadRequest("Destination options must be an object")
        clean: dict = {}
        for field in cls.FIELDS:
            key = field["key"]
            if key not in options:
                continue
            value = _option(field, options[key])
            if field["kind"] == "password" and value == "":
                value = _find_old_secret(old, index, cls.type, key)
            clean[key] = value
        out.append({"type": cls.type, "options": clean})
    return out


def _apply(profile: Profile, body: dict, old: Optional[Profile], others: list[Profile]) -> Profile:
    """`profile` changed by the keys in `body`; raises BadRequest on bad input."""
    if not isinstance(body, dict):
        raise BadRequest("Body must be a JSON object")
    p = copy.deepcopy(profile)
    if "name" in body or not p.name:
        p.name = _text(body, "name", "Name", MAX_NAME, required=True)
    if "icon" in body:
        p.icon = _text(body, "icon", "Icon", MAX_ICON)
    if "source" in body:
        p.source = _choice(body["source"], profiles.SOURCES, "Source")
    if "color_mode" in body:
        p.color_mode = _choice(body["color_mode"], profiles.COLOR_MODES, "Color")
    if "page_size" in body:
        p.page_size = _choice(body["page_size"], profiles.PAGE_SIZES, "Page size")
    if "split" in body:
        p.split = _choice(body["split"], profiles.SPLIT_MODES, "Split")
    if "resolution" in body:
        value = body["resolution"]
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if not isinstance(value, int) or isinstance(value, bool) \
                or not RESOLUTION_RANGE[0] <= value <= RESOLUTION_RANGE[1]:
            raise BadRequest(f"Resolution must be between {RESOLUTION_RANGE[0]} and "
                             f"{RESOLUTION_RANGE[1]}")
        p.resolution = value
    for key, label in (("duplex", "Both sides"), ("show_on_home", "Show on the dashboard")):
        if key in body:
            setattr(p, key, _bool(body[key], label))
    if "name_rule" in body:
        p.name_rule = _text(body, "name_rule", "File name", MAX_RULE, required=True)
    if "processing" in body:
        p.processing = _processing(body["processing"])
    if "destinations" in body:
        p.destinations = _destinations(body["destinations"], old)
    if not p.destinations:
        raise BadRequest("Add at least one destination")
    if any(o.id != p.id and o.name.strip().lower() == p.name.lower() for o in others):
        raise BadRequest(f"Another profile is already named {p.name!r}")
    p.follow_settings = False
    return p


# --- saving -------------------------------------------------------------------

def _editable(config):
    """A copy of `config` that has its stored profiles (migrated if needed)."""
    new = copy.deepcopy(config)
    if not new.profiles:
        profiles.migrate(new)
    return new


def _commit(old, new) -> None:
    from .settings_api import commit_config
    commit_config(old, new)
    # The feeder monitor keeps the config it was started with; give it the
    # new one so it scans with the edited default profile.
    try:
        from .. import monitor
        if monitor.monitor_running():
            monitor.start_monitor(new)
    except Exception as e:  # pragma: no cover - a monitor problem must not fail the save
        logger.warning("Could not pass the new profiles to the feeder monitor: %s", e)


def _index(config, profile_id: str) -> int:
    for i, p in enumerate(config.profiles):
        if p.id == profile_id:
            return i
    raise BadRequest("Profile not found", status=404)


def _get_stored(config, profile_id: str) -> Profile:
    found = next((p for p in _stored(config) if p.id == profile_id), None)
    if found is None:
        raise BadRequest("Profile not found", status=404)
    return found


# --- routes -------------------------------------------------------------------

@route("GET", "/api/profiles")
def api_list(handler, query):
    if getattr(handler, "request_role", None) == "scan":
        # No destination options at all for the scan token.
        handler._send_json(_scan_view(get_config()))
        return
    handler._send_json(_listing(get_config()))


@route("POST", "/api/profiles")
def api_create(handler, query):
    body = handler.read_json_body()
    with _edit_lock:
        old = get_config()
        new = _editable(old)
        if len(new.profiles) >= MAX_PROFILES:
            raise BadRequest(f"You can have at most {MAX_PROFILES} profiles")
        start = profiles.from_settings(new, id="", name="")
        start.destinations = start.destinations or [{"type": "folder", "options": {}}]
        profile = _apply(start, body, None, new.profiles)
        profile.id = profiles.new_id(profile.name)
        new.profiles.append(profile)
        _commit(old, new)
    handler._send_json({"profile": public_profile(new, profile)}, status=201)


@route("POST", "/api/profiles/reorder")
def api_reorder(handler, query):
    body = handler.read_json_body()
    ids = body.get("ids")
    if not isinstance(ids, list) or not all(isinstance(i, str) for i in ids):
        raise BadRequest("ids must be a list of profile ids")
    with _edit_lock:
        old = get_config()
        new = _editable(old)
        if sorted(ids) != sorted(p.id for p in new.profiles):
            raise BadRequest("ids must list every profile once")
        by_id = {p.id: p for p in new.profiles}
        new.profiles = [by_id[i] for i in ids]
        _commit(old, new)
    handler._send_json(_listing(new))


@route("POST", "/api/profiles/preview")
def api_preview(handler, query):
    """The file name a rule makes now. "text" stands in for recognized text."""
    body = handler.read_json_body()
    rule = _text(body, "name_rule", "File name", MAX_RULE) or naming.DEFAULT_RULE
    text = body.get("text") or ""
    if not isinstance(text, str):
        raise BadRequest("Sample text must be text")
    text = text[:SAMPLE_TEXT_LIMIT]
    config = get_config()
    profile = Profile(id="preview", name=_text(body, "name", "Name", MAX_NAME) or "Profile",
                      name_rule=rule)
    pages = [t for t in text.split("\f")] if text else []
    result = ProcessResult(pdf=b"", page_texts=pages)
    context = {"config": config, "profile": profile, "source": "adf", "started_by": "preview",
               "now": datetime.now(), "n": 1, "pages": max(1, len(pages))}
    fields = naming.collect_fields(result, context)
    handler._send_json({"filename": naming.render(rule, fields)})


@route("GET", "/api/profiles/")
def api_get(handler, query, path_suffix=""):
    profile_id = path_suffix.strip("/")
    if not profile_id or "/" in profile_id:
        raise BadRequest("Profile not found", status=404)
    config = get_config()
    handler._send_json({"profile": public_profile(config, _get_stored(config, profile_id))})


@route("POST", "/api/profiles/")
def api_profile_action(handler, query, path_suffix=""):
    parts = path_suffix.strip("/").split("/")
    if not parts[0] or len(parts) > 2:
        raise BadRequest("Not found", status=404)
    profile_id, action = parts[0], (parts[1] if len(parts) == 2 else "update")
    body = handler.read_json_body()
    if action == "scan":
        _scan(handler, profile_id, body)
        return
    if action not in ("update", "delete", "default"):
        raise BadRequest("Not found", status=404)
    with _edit_lock:
        old = get_config()
        new = _editable(old)
        i = _index(new, profile_id)
        if action == "update":
            current = new.profiles[i]
            new.profiles[i] = _apply(profiles.live(new, current), body, current, new.profiles)
        elif action == "delete":
            if len(new.profiles) == 1:
                raise BadRequest("You need at least one profile")
            del new.profiles[i]
            if new.default_profile_id == profile_id:
                new.default_profile_id = new.profiles[0].id
        else:
            new.default_profile_id = profile_id
        _commit(old, new)
    if action == "update":
        handler._send_json({"profile": public_profile(new, new.profiles[i])})
    else:
        handler._send_json(_listing(new))


def _scan(handler, profile_id: str, body: dict) -> None:
    from .dashboard_api import pick_source, send_scan
    config = get_config()
    profile = profiles.live(config, _get_stored(config, profile_id))
    send_scan(handler, pick_source(config, profile, body.get("source")), profile)


@route("GET", "/api/destinations")
def api_destinations(handler, query):
    # settings_fallback: an empty option uses the value from the Settings page.
    handler._send_json({"destinations": [
        dict(cls.describe(), settings_fallback=cls.type in destinations.SETTINGS_FALLBACK_TYPES)
        for cls in destinations.available()]})


@route("POST", "/api/destinations/test")
def api_destination_test(handler, query):
    body = handler.read_json_body()
    type_ = body.get("type")
    cls = destinations.get(type_) if isinstance(type_, str) else None
    if cls is None:
        raise BadRequest("Choose a destination type")
    config = get_config()
    old = None
    if isinstance(body.get("profile_id"), str) and body["profile_id"]:
        old = next((p for p in _stored(config) if p.id == body["profile_id"]), None)
    index = body.get("index") if isinstance(body.get("index"), int) else -1
    [dest] = _destinations([{"type": cls.type, "options": body.get("options") or {}}], None)
    for key in cls.secret_keys():
        if not dest["options"].get(key):
            dest["options"][key] = _find_old_secret(old, index, cls.type, key)
    options = destinations.resolve_options(cls.type, dest["options"], config)
    try:
        outcome = cls().test(options)
    except Exception as e:
        logger.warning("Destination test for %s failed: %s", cls.type, e)
        outcome = {"ok": False, "message": str(e)}
    handler._send_json({"ok": bool(outcome.get("ok")), "message": str(outcome.get("message", ""))})


@route("GET", "/api/naming/vendors")
def api_vendors(handler, query):
    handler._send_json({"vendors": list(get_config().naming.vendors)})


@route("POST", "/api/naming/vendors")
def api_save_vendors(handler, query):
    body = handler.read_json_body()
    vendors = _str_list(body.get("vendors", []), "Vendors")
    if len(vendors) > MAX_VENDORS or any(len(v) > 100 for v in vendors):
        raise BadRequest(f"Use at most {MAX_VENDORS} vendor names of 100 characters or fewer")
    seen, unique = set(), []
    for v in vendors:
        if v.lower() not in seen:
            seen.add(v.lower())
            unique.append(v)
    with _edit_lock:
        old = get_config()
        new = copy.deepcopy(old)
        new.naming.vendors = unique
        _commit(old, new)
    handler._send_json({"vendors": unique})
