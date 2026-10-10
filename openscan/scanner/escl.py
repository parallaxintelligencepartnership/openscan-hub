"""eSCL (AirScan) scanner protocol implementation.

eSCL is the protocol used by AirPrint/AirScan compatible printers.
Supported by HP, Canon, Epson, Brother, and most modern network scanners.
"""

import logging
import time
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from ..pages import combine_pages, finish_scan
from .base import (
    nearest_resolution,
    pick_color_mode,
    scan_region,
    AdfState,
    ScannerCapabilities,
    ScannerInfo,
    ScannerProtocol,
    ScannerState,
    ScannerStatus,
    ScanSettings,
    ScanSource,
)

logger = logging.getLogger(__name__)

SCAN_NS_URI = "http://schemas.hp.com/imaging/escl/2011/05/03"
PWG_NS_URI = "http://www.pwg.org/schemas/2010/12/sm"
ESCL_NS = {"scan": SCAN_NS_URI}
PWG_NS = {"pwg": PWG_NS_URI}

_STATE_MAP = {
    "Idle": ScannerState.IDLE,
    "Processing": ScannerState.PROCESSING,
    "Testing": ScannerState.PROCESSING,
    "Stopped": ScannerState.STOPPED,
    "Down": ScannerState.STOPPED,
}
# Valid eSCL ADF states that need no special handling (no warning).
_KNOWN_OTHER_ADF_STATES = {
    "ScannerAdfProcessing", "ScannerAdfHopperEmpty", "ScannerAdfMispick",
    "ScannerAdfDuplexPageTooShort", "ScannerAdfDuplexPageTooLong",
    "ScannerAdfMultipickDetected", "ScannerAdfInputTrayFailed",
    "ScannerAdfInputTrayOverloaded", "ScannerAdfHatchOpen",
}
_FORMAT_PREFERENCE = ["application/pdf", "image/jpeg", "image/png", "image/tiff"]
_warned: set[str] = set()


def _warn_once(key: str, msg: str, *args) -> None:
    if key not in _warned:
        _warned.add(key)
        logger.warning(msg, *args)


def _txt(props: dict, key: bytes) -> str:
    value = props.get(key)
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace").strip()
    return str(value).strip()


def _hint(code: int) -> str:
    if code == 409:
        return " (the scanner is busy with another job)"
    if code == 503:
        return " (the scanner is busy)"
    return ""


def _int(text: Optional[str]) -> int:
    try:
        return int((text or "").strip())
    except ValueError:
        return 0


def pick_format(formats: list[str]) -> str:
    """PDF if offered, else JPEG, else the first image type offered."""
    for candidate in _FORMAT_PREFERENCE:
        if candidate in formats:
            return candidate
    if formats:
        logger.warning("Scanner offers none of %s, requesting PDF anyway", _FORMAT_PREFERENCE)
    return "application/pdf"


def parse_capabilities(root: ET.Element) -> ScannerCapabilities:
    """Parse eSCL ScannerCapabilities XML. Missing pieces keep defaults."""
    ns = SCAN_NS_URI
    caps = ScannerCapabilities()
    input_caps: list[tuple[ScanSource, ET.Element]] = []
    platen = root.find(f"{{{ns}}}Platen")
    if platen is not None:
        pc = platen.find(f"{{{ns}}}PlatenInputCaps")
        input_caps.append((ScanSource.FLATBED, pc if pc is not None else platen))
    adf = root.find(f"{{{ns}}}Adf")
    if adf is not None:
        ac = adf.find(f"{{{ns}}}AdfSimplexInputCaps")
        if ac is None:
            ac = adf.find(f"{{{ns}}}AdfDuplexInputCaps")
        input_caps.append((ScanSource.ADF, ac if ac is not None else adf))
        options = [o.text.strip() for o in adf.iter(f"{{{ns}}}AdfOption") if o.text]
        caps.duplex = adf.find(f"{{{ns}}}AdfDuplexInputCaps") is not None or "Duplex" in options
    caps.sources = [src for src, _ in input_caps] or [ScanSource.FLATBED]

    resolutions: set[int] = set()
    formats: list[str] = []
    colors: list[str] = []
    for src, el in input_caps:
        w, h = _int(el.findtext(f"{{{ns}}}MaxWidth")), _int(el.findtext(f"{{{ns}}}MaxHeight"))
        if w > 0 and h > 0:
            caps.source_sizes[src.value] = (w, h)
        for r in el.iter(f"{{{ns}}}XResolution"):
            if _int(r.text) > 0:
                resolutions.add(_int(r.text))
        for tag in (f"{{{PWG_NS_URI}}}DocumentFormat", f"{{{ns}}}DocumentFormatExt"):
            for f in el.iter(tag):
                if f.text and f.text.strip() not in formats:
                    formats.append(f.text.strip())
        for c in el.iter(f"{{{ns}}}ColorMode"):
            if c.text and c.text.strip() not in colors:
                colors.append(c.text.strip())
    if resolutions:
        caps.resolutions = sorted(resolutions)
    if formats:
        caps.formats = formats
    if colors:
        caps.color_modes = colors
    first = caps.source_sizes.get(caps.sources[0].value)
    if first:
        caps.max_width, caps.max_height = first
    return caps


class EsclScanner(ScannerProtocol):
    """eSCL (AirScan) scanner protocol."""

    # Timing knobs (seconds). Tests shrink these.
    http_timeout = 60.0  # a NextDocument GET can block while a page scans
    retry_delay = 1.0
    page_timeout = 60.0  # max wait for one page while the scanner says 503
    busy_wait = 10.0
    max_create_attempts = 3
    max_pages = 500

    def discover(self, timeout: float = 5.0) -> list[ScannerInfo]:
        """Discover eSCL scanners via mDNS/Zeroconf."""
        scanners = []
        try:
            from zeroconf import ServiceBrowser, Zeroconf

            zc = Zeroconf()
            found = []

            class Listener:
                def add_service(self, zc, type_, name):
                    info = zc.get_service_info(type_, name)
                    if info:
                        found.append(info)

                def remove_service(self, zc, type_, name):
                    pass

                def update_service(self, zc, type_, name):
                    pass

            ServiceBrowser(zc, "_uscan._tcp.local.", Listener())
            time.sleep(timeout)
            zc.close()

            for info in found:
                addresses = info.parsed_addresses()
                if not addresses:
                    continue
                ipv4 = [a for a in addresses if ":" not in a]
                ip = (ipv4 or addresses)[0]
                port = info.port
                name = info.name
                props = info.properties or {}
                model = _txt(props, b"ty") or _txt(props, b"product")
                root_path = _txt(props, b"rs").strip("/")

                scanner = ScannerInfo(
                    name=name,
                    ip=ip,
                    port=port,
                    protocol="escl",
                    model=model,
                    path=f"/{root_path}" if root_path else "",
                )
                # Probe for capabilities to get sources
                try:
                    caps = self.get_capabilities(scanner)
                    scanner.sources = caps.sources
                except Exception:
                    scanner.sources = [ScanSource.FLATBED]

                scanners.append(scanner)

        except ImportError:
            logger.warning("zeroconf not installed - mDNS discovery unavailable")
        except Exception as e:
            logger.error("eSCL discovery failed: %s", e)

        return scanners

    # --- URLs ---

    def _root(self, scanner: ScannerInfo) -> str:
        """eSCL root path: the mDNS `rs` value when known, else /eSCL."""
        if scanner.protocol == "escl" and scanner.path:
            return "/" + scanner.path.strip("/")
        return "/eSCL"

    def _url(self, scanner: ScannerInfo, name: str) -> str:
        return f"{scanner.base_url}{self._root(scanner)}/{name}"

    # --- Status ---

    def get_status(self, scanner: ScannerInfo) -> ScannerStatus:
        """Get scanner status via eSCL ScannerStatus endpoint."""
        try:
            root = self._get_xml(self._url(scanner, "ScannerStatus"))
        except (URLError, OSError) as e:
            logger.debug("Cannot reach scanner %s: %s", scanner.ip, e)
            return ScannerStatus(state=ScannerState.OFFLINE)
        except ET.ParseError as e:
            _warn_once(f"status-xml-{scanner.ip}", "Scanner %s sent unreadable status: %s", scanner.ip, e)
            return ScannerStatus(state=ScannerState.IDLE)

        state = ScannerState.IDLE
        state_el = root.find(f".//{{{PWG_NS_URI}}}State")
        if state_el is not None and state_el.text:
            text = state_el.text.strip()
            state = _STATE_MAP.get(text)
            if state is None:
                _warn_once(f"state-{text}", "Unknown eSCL scanner state %r, treating as Idle", text)
                state = ScannerState.IDLE

        adf_state = AdfState.UNKNOWN
        adf_el = root.find(f".//{{{SCAN_NS_URI}}}AdfState")
        if adf_el is not None and adf_el.text:
            text = adf_el.text.strip()
            try:
                adf_state = AdfState(text)
            except ValueError:
                if text not in _KNOWN_OTHER_ADF_STATES:
                    _warn_once(f"adf-{text}", "Unknown eSCL ADF state %r", text)
        return ScannerStatus(state=state, adf_state=adf_state)

    def _job_state(self, scanner: ScannerInfo, job_url: str) -> str:
        """JobState of our job from ScannerStatus ('' when unknown)."""
        try:
            root = self._get_xml(self._url(scanner, "ScannerStatus"))
        except (URLError, OSError, ET.ParseError):
            return ""
        job_path = urlparse(job_url).path.rstrip("/")
        for info in root.iter(f"{{{SCAN_NS_URI}}}JobInfo"):
            uri = info.findtext(f"{{{PWG_NS_URI}}}JobUri") or ""
            if uri and urlparse(uri.strip()).path.rstrip("/") == job_path:
                return (info.findtext(f"{{{PWG_NS_URI}}}JobState") or "").strip()
        return ""

    # --- Capabilities ---

    def get_capabilities(self, scanner: ScannerInfo) -> ScannerCapabilities:
        """Get scanner capabilities via eSCL ScannerCapabilities endpoint."""
        try:
            root = self._get_xml(self._url(scanner, "ScannerCapabilities"))
        except (URLError, OSError, ET.ParseError) as e:
            logger.warning("Cannot get capabilities from %s: %s", scanner.ip, e)
            return ScannerCapabilities()
        try:
            return parse_capabilities(root)
        except Exception as e:  # unusual XML must never break scanning
            logger.warning("Could not read capabilities from %s, using defaults: %s", scanner.ip, e)
            return ScannerCapabilities()

    # --- Scanning ---

    def scan(
        self,
        scanner: ScannerInfo,
        settings: Optional[ScanSettings] = None,
    ) -> bytes:
        """Execute a scan via eSCL and return one PDF with every page."""
        if settings is None:
            settings = ScanSettings()

        caps = self.get_capabilities(scanner)
        if caps.sources and settings.source not in caps.sources:
            if settings.source == ScanSource.ADF:
                raise RuntimeError("This scanner has no document feeder.")
            raise RuntimeError("This scanner has no flatbed glass.")

        scan_xml = self._build_scan_request(settings, caps)
        job_url = self._create_scan_job(scanner, scan_xml)
        logger.info("Scan job created: %s", job_url)

        try:
            documents = self._collect_documents(
                scanner, job_url, multi=settings.source == ScanSource.ADF
            )
        finally:
            self._delete_job(job_url)

        resolution = nearest_resolution(settings.resolution, caps.resolutions)
        pdf = combine_pages(documents, resolution)
        pdf = finish_scan(pdf, resolution, settings.remove_blank_pages)
        logger.info("Scan complete: %d document(s), %d bytes", len(documents), len(pdf))
        return pdf

    def _build_scan_request(
        self, settings: ScanSettings, caps: Optional[ScannerCapabilities] = None
    ) -> bytes:
        """Build the eSCL ScanSettings XML from settings and device caps."""
        caps = caps or ScannerCapabilities(sources=[settings.source], formats=[])
        # Use Feeder (not Adf!) for ADF source - critical for HP compatibility
        input_source = settings.source.value
        width, height = scan_region(
            settings.page_size, 300, caps.max_size_for(settings.source)
        )
        resolution = nearest_resolution(settings.resolution, caps.resolutions)
        color_mode = pick_color_mode(settings.color_mode, caps.color_modes)
        doc_format = pick_format(caps.formats)
        duplex = ""
        if settings.duplex and settings.source == ScanSource.ADF:
            if caps.duplex:
                duplex = "\n    <scan:Duplex>true</scan:Duplex>"
            else:
                logger.warning("Two-sided scanning is not supported by this scanner; scanning one side")

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{SCAN_NS_URI}"
    xmlns:pwg="{PWG_NS_URI}">
    <pwg:Version>2.0</pwg:Version>
    <scan:Intent>Document</scan:Intent>
    <pwg:ScanRegions>
        <pwg:ScanRegion>
            <pwg:Height>{height}</pwg:Height>
            <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
            <pwg:Width>{width}</pwg:Width>
            <pwg:XOffset>0</pwg:XOffset>
            <pwg:YOffset>0</pwg:YOffset>
        </pwg:ScanRegion>
    </pwg:ScanRegions>
    <pwg:DocumentFormat>{doc_format}</pwg:DocumentFormat>
    <pwg:InputSource>{input_source}</pwg:InputSource>
    <scan:ColorMode>{color_mode}</scan:ColorMode>
    <scan:XResolution>{resolution}</scan:XResolution>
    <scan:YResolution>{resolution}</scan:YResolution>{duplex}
</scan:ScanSettings>"""
        return xml.encode("utf-8")

    def _create_scan_job(self, scanner: ScannerInfo, scan_xml: bytes) -> str:
        """POST a scan job and return the absolute job URL.

        409 (another job active) and 503 (busy) wait for the scanner to go
        idle, then retry, a bounded number of times.
        """
        url = self._url(scanner, "ScanJobs")
        last_code = 0
        for attempt in range(self.max_create_attempts):
            req = Request(url, data=scan_xml, method="POST",
                          headers={"Content-Type": "text/xml"})
            try:
                with urlopen(req, timeout=self.http_timeout) as resp:
                    location = (resp.headers.get("Location") or "").strip()
            except HTTPError as e:
                last_code = e.code
                if e.code in (409, 503) and attempt < self.max_create_attempts - 1:
                    logger.warning("Scanner busy (HTTP %d), waiting before retrying", e.code)
                    self._wait_until_idle(scanner)
                    continue
                raise RuntimeError(f"Failed to create scan job: HTTP {e.code}{_hint(e.code)}") from e
            except (URLError, OSError) as e:
                raise RuntimeError(f"Cannot reach the scanner: {e}") from e
            if not location:
                raise RuntimeError("Scanner accepted the job but gave no job address")
            if not location.startswith("http"):
                location = f"{scanner.base_url}/{location.lstrip('/')}"
            return location.rstrip("/")
        raise RuntimeError(f"Failed to create scan job: HTTP {last_code}{_hint(last_code)}")

    def _wait_until_idle(self, scanner: ScannerInfo) -> None:
        deadline = time.monotonic() + self.busy_wait
        while time.monotonic() < deadline:
            time.sleep(self.retry_delay)
            if self.get_status(scanner).state != ScannerState.PROCESSING:
                return

    def _collect_documents(self, scanner: ScannerInfo, job_url: str, multi: bool) -> list[bytes]:
        """Fetch NextDocument until 404 (feeder) or once (flatbed)."""
        next_url = f"{job_url}/NextDocument"
        documents: list[bytes] = []
        waited = 0.0
        while len(documents) < self.max_pages:
            try:
                with urlopen(Request(next_url, method="GET"), timeout=self.http_timeout) as resp:
                    documents.append(resp.read())
                waited = 0.0
                if not multi:
                    break
                continue
            except HTTPError as e:
                code = e.code
            except (URLError, OSError) as e:
                raise RuntimeError(f"Lost connection to the scanner while scanning: {e}") from e

            if code in (404, 410):
                if documents:
                    break
                raise RuntimeError("The scanner returned no pages. Check that paper is loaded.")
            if code == 409:
                state = self._job_state(scanner, job_url)
                if state in ("Canceled", "Aborted"):
                    raise RuntimeError(f"The scanner stopped the job ({state.lower()}).")
                if state == "Completed":
                    if documents:
                        break
                    raise RuntimeError("The scanner finished the job without sending any pages.")
            elif code != 503:
                raise RuntimeError(f"Failed to download scan: HTTP {code}")
            if waited >= self.page_timeout:
                raise RuntimeError("Timed out waiting for the scanner to send the next page.")
            time.sleep(self.retry_delay)
            waited += self.retry_delay
        return documents

    def _delete_job(self, job_url: str) -> None:
        """Best-effort DELETE so the next job is not refused with 409."""
        try:
            with urlopen(Request(job_url, method="DELETE"), timeout=5):
                pass
        except HTTPError as e:
            logger.debug("Job DELETE returned HTTP %d (ignored)", e.code)
        except (URLError, OSError) as e:
            logger.debug("Job DELETE failed (ignored): %s", e)

    def _get_xml(self, url: str) -> ET.Element:
        with urlopen(Request(url, method="GET"), timeout=5) as resp:
            return ET.fromstring(resp.read())

    def test_connection(self, scanner: ScannerInfo) -> bool:
        """Test if a scanner is reachable via eSCL."""
        try:
            status = self.get_status(scanner)
            return status.state != ScannerState.OFFLINE
        except Exception:
            return False
