"""Abstract scanner protocol interface."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


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
    max_width: int = 2550  # 8.5" at 300dpi
    max_height: int = 3300  # 11" at 300dpi
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
    color_mode: str = "RGB24"
    format: str = "application/pdf"
    duplex: bool = False


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
