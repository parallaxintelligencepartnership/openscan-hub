"""Configuration management for OpenScanHub."""

import json
import logging
import os
import platform
import secrets
import sys
import tempfile
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ScannerConfig:
    ip: str = ""
    port: int = 0
    protocol: str = "escl"
    name: str = ""
    model: str = ""
    path: str = ""  # WSD: resolved scan service path


@dataclass
class OutputConfig:
    folder: str = ""
    filename_pattern: str = "scan_{date}_{time}_{n}"


@dataclass
class PaperlessConfig:
    enabled: bool = False
    mode: str = "consume"  # "consume" (folder drop) or "api"
    consume_folder: str = ""
    api_url: str = ""
    api_token: str = ""
    tags: list[str] = field(default_factory=list)
    correspondent: str = ""
    document_type: str = ""
    keep_local_copy: bool = True


@dataclass
class MonitorConfig:
    enabled: bool = False
    poll_interval: float = 3.0
    settle_time: float = 1.0


@dataclass
class FolderWatchConfig:
    enabled: bool = False
    watch_folder: str = ""
    extensions: list[str] = field(default_factory=lambda: [".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"])
    poll_interval: float = 5.0


@dataclass
class FtpReceiveConfig:
    enabled: bool = False
    port: int = 2121
    username: str = "scan"
    password: str = ""  # generated on first load if empty
    allow_anonymous: bool = False
    staging_dir: str = ""  # Temp dir for incoming files; auto-set if empty
    delete_after_routing: bool = True
    passive_ports: str = "60000-60010"
    # Address put in passive-mode replies ("" = this computer's main IPv4
    # address). Docker bridge mode needs the host's LAN address here.
    passive_address: str = ""


@dataclass
class WebConfig:
    bind: str = "127.0.0.1"  # "0.0.0.0" for LAN access
    port: int = 8020
    api_token: str = ""  # generated on first load
    advertise_mdns: bool = True


@dataclass
class ScanDefaults:
    resolution: int = 300
    color_mode: str = "color"  # color | gray | bw
    page_size: str = "auto"  # auto | letter | a4 | legal
    duplex: bool = False
    remove_blank_pages: bool = False


@dataclass
class HomeAssistantConfig:
    enabled: bool = False


@dataclass
class AppConfig:
    wizard_completed: bool = False
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    paperless: PaperlessConfig = field(default_factory=PaperlessConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    folder_watch: FolderWatchConfig = field(default_factory=FolderWatchConfig)
    ftp_receive: FtpReceiveConfig = field(default_factory=FtpReceiveConfig)
    web: WebConfig = field(default_factory=WebConfig)
    scan: ScanDefaults = field(default_factory=ScanDefaults)
    homeassistant: HomeAssistantConfig = field(default_factory=HomeAssistantConfig)
    log_level: str = "INFO"

    @property
    def web_port(self) -> int:
        return self.web.port

    @web_port.setter
    def web_port(self, value: int) -> None:
        self.web.port = value


def get_config_dir() -> Path:
    """Get the platform-appropriate config directory."""
    if os.environ.get("OPENSCAN_CONFIG_DIR"):
        return Path(os.environ["OPENSCAN_CONFIG_DIR"])

    if _is_docker():
        return Path("/config")

    system = platform.system()
    if system == "Windows":
        base = os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")
        return Path(base) / "OpenScanHub"
    elif system == "Darwin":
        return Path.home() / "Library" / "Application Support" / "OpenScanHub"
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
        return Path(xdg) / "openscan"


def get_config_path() -> Path:
    return get_config_dir() / "openscan.json"


def _is_docker() -> bool:
    """Detect if running inside a Docker container."""
    return (
        os.path.exists("/.dockerenv")
        or os.environ.get("OPENSCAN_DOCKER") == "1"
    )


def _is_frozen() -> bool:
    """Detect if running as a PyInstaller bundle."""
    return getattr(sys, "frozen", False)


def load_config(path: Optional[Path] = None) -> AppConfig:
    """Load config from JSON file, returning defaults if not found or broken."""
    config_path = path or get_config_path()
    config = None
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            config = _dict_to_config(data)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError,
                ValueError, AttributeError) as e:
            logger.warning("Failed to load config from %s: %s", config_path, e)
            _keep_broken_copy(config_path)
    if config is None:
        config = AppConfig()

    generated = False
    if not config.web.api_token:
        config.web.api_token = secrets.token_hex(16)
        generated = True
    if config.ftp_receive.password in ("", "scan"):
        config.ftp_receive.password = secrets.token_urlsafe(12)
        generated = True
    if generated:
        try:
            save_config(config, config_path)
        except OSError as e:
            logger.warning("Could not persist generated secrets to %s: %s", config_path, e)

    # Environment overrides (not persisted)
    if os.environ.get("OPENSCAN_BIND"):
        config.web.bind = os.environ["OPENSCAN_BIND"]
    if os.environ.get("OPENSCAN_PORT"):
        try:
            config.web.port = int(os.environ["OPENSCAN_PORT"])
        except ValueError:
            logger.warning("Ignoring invalid OPENSCAN_PORT=%r", os.environ["OPENSCAN_PORT"])
    if os.environ.get("OPENSCAN_TOKEN"):
        config.web.api_token = os.environ["OPENSCAN_TOKEN"]
    if os.environ.get("OPENSCAN_FTP_PASSIVE_ADDRESS"):
        config.ftp_receive.passive_address = os.environ["OPENSCAN_FTP_PASSIVE_ADDRESS"].strip()
    return config


def _keep_broken_copy(config_path: Path) -> None:
    """Move an unreadable config aside so the defaults written next do not destroy it."""
    backup = config_path.with_name(config_path.name + ".broken")
    try:
        os.replace(config_path, backup)
        logger.warning("Kept the unreadable config as %s; starting with default settings", backup)
    except OSError as e:
        logger.warning("Could not move the unreadable config aside: %s", e)


def save_config(config: AppConfig, path: Optional[Path] = None) -> None:
    """Save config to JSON file atomically, readable only by the owner."""
    config_path = path or get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(config_path.parent), prefix=config_path.name + ".", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, config_path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    logger.info("Config saved to %s", config_path)


def _section(cls, raw, name: str):
    """Build dataclass `cls` from dict `raw`, ignoring unknown keys and
    falling back to the default for any value of the wrong type."""
    obj = cls()
    if raw is None:
        return obj
    if not isinstance(raw, dict):
        logger.warning("Config section %r should be an object; using defaults", name)
        return obj
    known = {f.name for f in fields(cls)}
    for key, value in raw.items():
        if key not in known:
            logger.warning("Ignoring unknown config key %s.%s", name, key)
            continue
        default = getattr(obj, key)
        if isinstance(default, bool):
            ok = isinstance(value, bool)
        elif isinstance(default, int):
            ok = isinstance(value, int) and not isinstance(value, bool)
        elif isinstance(default, float):
            ok = isinstance(value, (int, float)) and not isinstance(value, bool)
        elif isinstance(default, str):
            ok = isinstance(value, str)
        elif isinstance(default, list):
            ok = isinstance(value, list) and all(isinstance(v, str) for v in value)
        else:
            ok = True
        if not ok:
            logger.warning("Config key %s.%s has wrong type; using default", name, key)
            continue
        if isinstance(default, float):
            value = float(value)
        setattr(obj, key, value)
    return obj


def _dict_to_config(data: dict) -> AppConfig:
    """Reconstruct AppConfig from a dict, tolerating unknown keys and bad types."""
    if not isinstance(data, dict):
        raise TypeError("config root must be an object")
    config = AppConfig()
    if isinstance(data.get("wizard_completed"), bool):
        config.wizard_completed = data["wizard_completed"]
    if isinstance(data.get("log_level"), str):
        config.log_level = data["log_level"]

    sections = {
        "scanner": ScannerConfig,
        "output": OutputConfig,
        "paperless": PaperlessConfig,
        "monitor": MonitorConfig,
        "folder_watch": FolderWatchConfig,
        "ftp_receive": FtpReceiveConfig,
        "web": WebConfig,
        "scan": ScanDefaults,
        "homeassistant": HomeAssistantConfig,
    }
    for name, cls in sections.items():
        raw = data.get(name)
        if name == "paperless" and isinstance(raw, dict):
            raw = dict(raw)
            if "default_tags" in raw:
                raw.setdefault("tags", raw["default_tags"])
                del raw["default_tags"]
        setattr(config, name, _section(cls, raw, name))

    # Migrate old top-level web_port
    old_port = data.get("web_port")
    if isinstance(old_port, int) and not isinstance(old_port, bool):
        if not (isinstance(data.get("web"), dict) and "port" in data["web"]):
            config.web.port = old_port

    return config
