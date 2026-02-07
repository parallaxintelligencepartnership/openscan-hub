"""Configuration management for OpenScanHub."""

import json
import logging
import os
import platform
import sys
from dataclasses import asdict, dataclass, field
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
    default_tags: list[str] = field(default_factory=list)


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
class AppConfig:
    wizard_completed: bool = False
    scanner: ScannerConfig = field(default_factory=ScannerConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    paperless: PaperlessConfig = field(default_factory=PaperlessConfig)
    monitor: MonitorConfig = field(default_factory=MonitorConfig)
    folder_watch: FolderWatchConfig = field(default_factory=FolderWatchConfig)
    web_port: int = 8020
    log_level: str = "INFO"


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
    """Load config from JSON file, returning defaults if not found."""
    config_path = path or get_config_path()
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            return _dict_to_config(data)
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning("Failed to load config from %s: %s", config_path, e)
    return AppConfig()


def save_config(config: AppConfig, path: Optional[Path] = None) -> None:
    """Save config to JSON file."""
    config_path = path or get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    data = asdict(config)
    config_path.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8",
    )
    logger.info("Config saved to %s", config_path)


def _dict_to_config(data: dict) -> AppConfig:
    """Reconstruct AppConfig from a dict, handling missing keys gracefully."""
    config = AppConfig()
    config.wizard_completed = data.get("wizard_completed", False)
    config.web_port = data.get("web_port", 8020)
    config.log_level = data.get("log_level", "INFO")

    if "scanner" in data:
        s = data["scanner"]
        config.scanner = ScannerConfig(
            ip=s.get("ip", ""),
            port=s.get("port", 0),
            protocol=s.get("protocol", "escl"),
            name=s.get("name", ""),
            model=s.get("model", ""),
        )

    if "output" in data:
        o = data["output"]
        config.output = OutputConfig(
            folder=o.get("folder", ""),
            filename_pattern=o.get("filename_pattern", "scan_{date}_{time}_{n}"),
        )

    if "paperless" in data:
        p = data["paperless"]
        config.paperless = PaperlessConfig(
            enabled=p.get("enabled", False),
            mode=p.get("mode", "consume"),
            consume_folder=p.get("consume_folder", ""),
            api_url=p.get("api_url", ""),
            api_token=p.get("api_token", ""),
            default_tags=p.get("default_tags", []),
        )

    if "monitor" in data:
        m = data["monitor"]
        config.monitor = MonitorConfig(
            enabled=m.get("enabled", False),
            poll_interval=m.get("poll_interval", 3.0),
            settle_time=m.get("settle_time", 1.0),
        )

    if "folder_watch" in data:
        f = data["folder_watch"]
        config.folder_watch = FolderWatchConfig(
            enabled=f.get("enabled", False),
            watch_folder=f.get("watch_folder", ""),
            extensions=f.get("extensions", [".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"]),
            poll_interval=f.get("poll_interval", 5.0),
        )

    return config
