"""Settings, sign-in, token, recent problems and retry queue endpoints."""

import copy
import ipaddress
import logging
import os
import platform
import secrets
import subprocess
import time
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from .. import services
from ..config import AppConfig, _is_docker, save_config
from ..logging_setup import get_log_dir, recent_errors
from .server import BadRequest, get_config, route, secret_equal, set_config

logger = logging.getLogger(__name__)

SECTIONS = ("scanner", "output", "paperless", "monitor", "folder_watch",
            "ftp_receive", "web", "scan", "homeassistant")

# Blank means "keep the current value".
SECRET_FIELDS = {("paperless", "api_token"), ("ftp_receive", "password")}
# Never set through settings (the token has its own regenerate route).
READ_ONLY_FIELDS = {("web", "api_token"), ("ftp_receive", "staging_dir")}

CHOICES = {
    ("scanner", "protocol"): {"escl", "wsd", "ftp", "folder", ""},
    ("paperless", "mode"): {"consume", "api"},
    ("scan", "color_mode"): {"color", "gray", "bw"},
    ("scan", "page_size"): {"auto", "letter", "a4", "legal"},
}
RANGES = {
    ("web", "port"): (1, 65535),
    ("ftp_receive", "port"): (1, 65535),
    ("scanner", "port"): (0, 65535),
    ("scan", "resolution"): (75, 1200),
    ("monitor", "poll_interval"): (1, 3600),
    ("monitor", "settle_time"): (0, 60),
    ("folder_watch", "poll_interval"): (1, 3600),
}
LABELS = {
    "output.folder": "Folder",
    "web.port": "Web port",
    "ftp_receive.port": "FTP port",
    "scan.resolution": "Resolution",
    "paperless.api_url": "Paperless-NGX address",
    "web.bind": "Network access",
}


def _label(section: str, key: str) -> str:
    return LABELS.get(f"{section}.{key}", f"{section}.{key}")


def _coerce(section: str, key: str, default: Any, value: Any) -> Any:
    name = _label(section, key)
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise BadRequest(f"{name} must be on or off")
        return value
    if isinstance(default, int):
        if isinstance(value, bool):
            raise BadRequest(f"{name} must be a whole number")
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        try:
            value = int(str(value).strip()) if not isinstance(value, int) else value
        except ValueError:
            raise BadRequest(f"{name} must be a whole number")
    elif isinstance(default, float):
        if isinstance(value, bool):
            raise BadRequest(f"{name} must be a number")
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise BadRequest(f"{name} must be a number")
    elif isinstance(default, str):
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise BadRequest(f"{name} must be text")
        value = value.strip()
    elif isinstance(default, list):
        if isinstance(value, str):
            value = [v.strip() for v in value.split(",") if v.strip()]
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise BadRequest(f"{name} must be a list")
        value = [v.strip() for v in value if v.strip()]
    rng = RANGES.get((section, key))
    if rng and not (rng[0] <= value <= rng[1]):
        raise BadRequest(f"{name} must be between {rng[0]} and {rng[1]}")
    choices = CHOICES.get((section, key))
    if choices is not None and value not in choices:
        raise BadRequest(f"{name} has an unknown value")
    return value


def _validate(cfg: AppConfig) -> None:
    url = cfg.paperless.api_url
    if url and not url.lower().startswith(("http://", "https://")):
        raise BadRequest("Paperless-NGX address must start with http:// or https://")
    bind = cfg.web.bind
    if bind != "localhost":
        try:
            ipaddress.ip_address(bind)
        except ValueError:
            raise BadRequest("Network access setting is not a valid address")
    passive = (cfg.ftp_receive.passive_address or "").strip()
    if passive:
        try:
            ipaddress.IPv4Address(passive)
        except ValueError:
            raise BadRequest("The FTP address for the scanner must be an IPv4 address, "
                             "for example 192.168.1.20")


def merge_config(current: AppConfig, body: dict) -> AppConfig:
    """Return a copy of `current` with the known fields from `body` applied.

    Unknown keys are ignored, secrets left blank keep their value, and nothing
    that is not in `body` changes.
    """
    new = copy.deepcopy(current)
    for section in SECTIONS:
        raw = body.get(section)
        if raw is None:
            continue
        if not isinstance(raw, dict):
            raise BadRequest(f"{section} must be an object")
        obj = getattr(new, section)
        known = {f.name for f in fields(obj)}
        for key, value in raw.items():
            if key not in known or (section, key) in READ_ONLY_FIELDS:
                continue
            if (section, key) in SECRET_FIELDS and (value is None or value == ""):
                continue
            setattr(obj, key, _coerce(section, key, getattr(obj, key), value))
    if "log_level" in body:
        level = str(body["log_level"]).upper()
        if level not in ("DEBUG", "INFO", "WARNING", "ERROR"):
            raise BadRequest("Log level has an unknown value")
        new.log_level = level
    if new.output.folder.startswith("~"):
        new.output.folder = str(Path(new.output.folder).expanduser())
    _validate(new)
    return new


def ensure_folder(folder: str) -> str:
    """Create the output folder. Returns an error message, or "" on success."""
    if not folder:
        return ""
    try:
        Path(folder).mkdir(parents=True, exist_ok=True)
        return ""
    except OSError as e:
        return f"Could not create the folder {folder}: {e.strerror or e}"


def commit_config(old: AppConfig, new: AppConfig) -> dict:
    """Save, make live, and restart services. Returns the services report."""
    try:
        save_config(new)
    except OSError as e:
        raise BadRequest(f"Could not save settings: {e.strerror or e}", status=500)
    set_config(new)
    return services.apply_config(old, new)


def redacted_config(cfg: AppConfig) -> dict:
    data = asdict(cfg)
    data["paperless"]["has_token"] = bool(cfg.paperless.api_token)
    data["paperless"]["api_token"] = ""
    data["ftp_receive"]["has_password"] = bool(cfg.ftp_receive.password)
    data["ftp_receive"]["password"] = ""
    data["web"]["has_token"] = bool(cfg.web.api_token)
    data["web"]["api_token"] = ""
    return data


# --- routes ------------------------------------------------------------------

@route("POST", "/api/login", public=True)
def api_login(handler, query):
    body = handler.read_json_body()
    supplied = str(body.get("token", "")).strip()
    token = get_config().web.api_token
    if not supplied or not secret_equal(supplied, token):
        time.sleep(0.5)  # slows guessing; the token is 128 bits anyway
        handler._send_json({"ok": False, "error": "That access token is not right"}, status=401)
        return
    handler.add_header("Set-Cookie", handler.session_cookie_header())
    handler._send_json({"ok": True})


@route("GET", "/api/config")
def api_get_config(handler, query):
    cfg = get_config()
    data = redacted_config(cfg)
    data["restart_needed"] = services.restart_needed(cfg)
    log_dir = get_log_dir()
    data["log_folder"] = str(log_dir) if log_dir else ""
    data["platform"] = platform.system()
    data["is_docker"] = _is_docker()
    data["local_request"] = handler.is_loopback_peer()
    handler._send_json(data)


@route("GET", "/api/secrets")
def api_secrets(handler, query):
    cfg = get_config()
    handler._send_json({"api_token": cfg.web.api_token, "ftp_password": cfg.ftp_receive.password})


@route("POST", "/api/settings")
def api_save_settings(handler, query):
    body = handler.read_json_body()
    old = get_config()
    new = merge_config(old, body)
    warnings = []
    if new.output.folder != old.output.folder:
        problem = ensure_folder(new.output.folder)
        if problem:
            warnings.append(problem)
    report = commit_config(old, new)
    handler._send_json({
        "saved": True,
        "restart_needed": report["restart_needed"],
        "warnings": warnings + report["errors"],
    })


@route("POST", "/api/token/regenerate")
def api_regenerate_token(handler, query):
    handler.read_json_body()
    old = get_config()
    new = copy.deepcopy(old)
    new.web.api_token = secrets.token_hex(16)
    commit_config(old, new)
    logger.warning("Access token was regenerated; other devices must sign in again")
    handler.add_header("Set-Cookie", handler.session_cookie_header())
    handler._send_json({"api_token": new.web.api_token})


@route("GET", "/api/errors")
def api_errors(handler, query):
    try:
        limit = max(1, min(200, int(query.get("limit", [50])[0])))
    except ValueError:
        limit = 50
    handler._send_json({"errors": recent_errors(limit)})


@route("GET", "/api/queue")
def api_queue(handler, query):
    from .dashboard_api import queue_pending
    handler._send_json({"pending": queue_pending()})


@route("POST", "/api/queue/retry")
def api_queue_retry(handler, query):
    """Body: {"id": "..."} retries one item; {} retries all."""
    body = handler.read_json_body()
    try:
        from .. import retry_queue
        queue = retry_queue.get_queue()
    except (ImportError, AttributeError):
        handler._send_json({"error": "Retry queue is not available"}, status=503)
        return
    item_id = body.get("id")
    if item_id:
        handler._send_json({"retried": 1 if queue.retry_now(str(item_id)) else 0})
    else:
        handler._send_json({"retried": int(queue.retry_all())})


@route("POST", "/api/open-log-folder")
def api_open_log_folder(handler, query):
    """Open the log folder in the file manager. Only for a browser on this computer."""
    handler.read_json_body()
    log_dir = get_log_dir()
    if not handler.is_loopback_peer() or _is_docker() or log_dir is None:
        handler._send_json({"opened": False, "folder": str(log_dir or "")})
        return
    handler._send_json({"opened": open_folder(log_dir), "folder": str(log_dir)})


def open_folder(path: Path) -> bool:
    try:
        if os.name == "nt":
            os.startfile(str(path))  # type: ignore[attr-defined]
        elif platform.system() == "Darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except (OSError, AttributeError) as e:
        logger.warning("Could not open %s: %s", path, e)
        return False
