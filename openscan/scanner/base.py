"""Abstract scanner protocol interface."""

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


scan_lock = threading.RLock()


def try_acquire_scan(timeout: float = 0.0) -> bool:
    """Try to take the global scanner lock. Returns True if acquired."""
    if timeout and timeout > 0:
        return scan_lock.acquire(timeout=timeout)
    return scan_lock.acquire(blocking=False)


def release_scan() -> None:
    """Release the global scanner lock."""
    try:
        scan_lock.release()
    except RuntimeError:
        pass


_COLOR_MODE_MAP = {
    "color": "RGB24",
    "gray": "Grayscale8",
    "bw": "BlackAndWhite1",
}


def to_protocol_color_mode(color_mode: str) -> str:
    """Map "color"/"gray"/"bw" to eSCL/WSD names. Native names pass through."""
    return _COLOR_MODE_MAP.get(color_mode, color_mode or "RGB24")


class ScanSource(Enum):
    FLATBED = "Platen"
    ADF = "Feeder"


class AdfState(Enum):
    UNKNOWN = "Unknown"
    EMPTY = "ScannerAdfEmpty"
    LOADED = "ScannerAdfLoaded"
    JAMMED = "ScannerAdfJam"


class ScannerState(Enum):
    IDLE = "Idle"
    PROCESSING = "Processing"
    STOPPED = "Stopped"
    OFFLINE = "Offline"


@dataclass
class ScannerInfo:
    name: str
    ip: str
    port: int
    protocol: str  # "escl", "wsd", "folder"
    model: str = ""
    sources: list[ScanSource] = field(default_factory=list)
    uuid: str = ""
    path: str = ""  # WSD: resolved scan service path, e.g. "/wsd/scanservice.cgi"

    @property
    def base_url(self) -> str:
        return f"http://{self.ip}:{self.port}"

    @property
    def display_name(self) -> str:
        return self.model or self.name

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "ip": self.ip,
            "port": self.port,
            "protocol": self.protocol,
            "model": self.model,
            "sources": [s.value for s in self.sources],
            "uuid": self.uuid,
            "path": self.path,
            "display_name": self.display_name,
        }


@dataclass
class ScannerStatus:
    state: ScannerState = ScannerState.OFFLINE
    adf_state: AdfState = AdfState.UNKNOWN

    def to_dict(self) -> dict:
        return {
            "state": self.state.value,
            "adf_state": self.adf_state.value,
        }


@dataclass
class ScannerCapabilities:
    resolutions: list[int] = field(default_factory=lambda: [300])
    sources: list[ScanSource] = field(default_factory=list)
    formats: list[str] = field(default_factory=lambda: ["application/pdf"])
    color_modes: list[str] = field(default_factory=lambda: ["RGB24"])
    max_width: int = 0  # native scan units; 0 = unknown/unreported
    max_height: int = 0  # native scan units; 0 = unknown/unreported
    duplex: bool = False

    def to_dict(self) -> dict:
        return {
            "resolutions": self.resolutions,
            "sources": [s.value for s in self.sources],
            "formats": self.formats,
            "color_modes": self.color_modes,
            "max_width": self.max_width,
            "max_height": self.max_height,
            "duplex": self.duplex,
        }


@dataclass
class ScanSettings:
    source: ScanSource = ScanSource.FLATBED
    resolution: int = 300
    color_mode: str = "color"  # color | gray | bw
    format: str = "application/pdf"
    duplex: bool = False
    page_size: str = "auto"  # auto | letter | a4 | legal
    remove_blank_pages: bool = False


class ScannerProtocol(ABC):
    """Abstract base class for scanner protocols."""

    @abstractmethod
    def discover(self, timeout: float = 5.0) -> list[ScannerInfo]:
        """Find scanners on the network using this protocol."""

    @abstractmethod
    def get_status(self, scanner: ScannerInfo) -> ScannerStatus:
        """Get current scanner status."""

    @abstractmethod
    def get_capabilities(self, scanner: ScannerInfo) -> ScannerCapabilities:
        """Get scanner capabilities (resolutions, sources, formats)."""

    @abstractmethod
    def scan(
        self,
        scanner: ScannerInfo,
        settings: Optional[ScanSettings] = None,
    ) -> bytes:
        """Execute a scan and return the resulting PDF bytes."""
