"""Abstract scanner protocol interface."""

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger(__name__)


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


_COLOR_FALLBACK = {
    "RGB24": ["Grayscale8", "BlackAndWhite1"],
    "Grayscale8": ["RGB24", "BlackAndWhite1"],
    "BlackAndWhite1": ["Grayscale8", "RGB24"],
}


def pick_color_mode(color_mode: str, supported: list[str]) -> str:
    """Protocol color mode for `color_mode`, falling back to the closest one the device has."""
    wanted = to_protocol_color_mode(color_mode)
    if not supported or wanted in supported:
        return wanted
    for candidate in _COLOR_FALLBACK.get(wanted, []) + list(supported):
        if candidate in supported:
            logger.warning("Color mode %s not supported by scanner, using %s", wanted, candidate)
            return candidate
    return wanted


def nearest_resolution(requested: int, supported: list[int]) -> int:
    """Closest supported resolution (ties go to the higher one)."""
    if not supported or requested in supported:
        return requested
    best = min(supported, key=lambda r: (abs(r - requested), -r))
    logger.info("Resolution %d dpi not supported by scanner, using %d dpi", requested, best)
    return best


# Paper sizes in inches (width, height).
PAGE_SIZES_INCHES = {
    "letter": (8.5, 11.0),
    "a4": (210 / 25.4, 297 / 25.4),
    "legal": (8.5, 14.0),
}


def scan_region(page_size: str, units_per_inch: int, max_size: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """Scan area (width, height) in device units for a page size setting.

    "auto" uses the source maximum; a named size is clamped to it. When the
    device reports no maximum, Letter is used for "auto".
    """
    max_w, max_h = max_size
    key = (page_size or "auto").lower()
    if key == "auto" and max_w > 0 and max_h > 0:
        return max_w, max_h
    if key not in PAGE_SIZES_INCHES:
        if key != "auto":
            logger.warning("Unknown page size %r, using the scanner maximum", page_size)
            if max_w > 0 and max_h > 0:
                return max_w, max_h
        key = "letter"
    w_in, h_in = PAGE_SIZES_INCHES[key]
    w, h = round(w_in * units_per_inch), round(h_in * units_per_inch)
    if max_w > 0:
        w = min(w, max_w)
    if max_h > 0:
        h = min(h, max_h)
    return w, h


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
        host = f"[{self.ip}]" if ":" in self.ip and not self.ip.startswith("[") else self.ip
        return f"http://{host}:{self.port}"

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
    # Per-source maximum scan area in native units, keyed by ScanSource.value.
    source_sizes: dict[str, tuple[int, int]] = field(default_factory=dict)
    auto_detect: bool = False  # device can detect the document size itself

    def max_size_for(self, source: "ScanSource") -> tuple[int, int]:
        size = self.source_sizes.get(source.value)
        if size and size[0] > 0 and size[1] > 0:
            return size
        return self.max_width, self.max_height

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
