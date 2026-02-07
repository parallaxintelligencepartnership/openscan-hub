"""eSCL (AirScan) scanner protocol implementation.

eSCL is the protocol used by AirPrint/AirScan compatible printers.
Supported by HP, Canon, Epson, Brother, and most modern network scanners.
"""

import logging
import time
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .base import (
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

ESCL_NS = {"scan": "http://schemas.hp.com/imaging/escl/2011/05/03"}
PWG_NS = {"pwg": "http://www.pwg.org/schemas/2010/12/sm"}

SCAN_NS_URI = "http://schemas.hp.com/imaging/escl/2011/05/03"


class EsclScanner(ScannerProtocol):
    """eSCL (AirScan) scanner protocol."""

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
                ip = addresses[0]
                port = info.port
                name = info.name
                model = ""
                if info.properties:
                    model = info.properties.get(b"ty", b"").decode("utf-8", errors="replace")
                    if not model:
                        model = info.properties.get(b"product", b"").decode("utf-8", errors="replace")

                scanner = ScannerInfo(
                    name=name,
                    ip=ip,
                    port=port,
                    protocol="escl",
                    model=model,
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

    def get_status(self, scanner: ScannerInfo) -> ScannerStatus:
        """Get scanner status via eSCL ScannerStatus endpoint."""
        url = f"{scanner.base_url}/eSCL/ScannerStatus"
        try:
            req = Request(url, method="GET")
            with urlopen(req, timeout=5) as resp:
                xml_data = resp.read()

            root = ET.fromstring(xml_data)
            state_el = root.find(".//pwg:State", PWG_NS)
            if state_el is None:
                state_el = root.find(".//{http://www.pwg.org/schemas/2010/12/sm}State")

            state = ScannerState.IDLE
            if state_el is not None and state_el.text:
                try:
                    state = ScannerState(state_el.text)
                except ValueError:
                    state = ScannerState.IDLE

            adf_state = AdfState.UNKNOWN
            adf_el = root.find(f".//{{{SCAN_NS_URI}}}AdfState")
            if adf_el is not None and adf_el.text:
                try:
                    adf_state = AdfState(adf_el.text)
                except ValueError:
                    adf_state = AdfState.UNKNOWN

            return ScannerStatus(state=state, adf_state=adf_state)

        except (URLError, OSError) as e:
            logger.warning("Cannot reach scanner %s: %s", scanner.ip, e)
            return ScannerStatus(state=ScannerState.OFFLINE)

    def get_capabilities(self, scanner: ScannerInfo) -> ScannerCapabilities:
        """Get scanner capabilities via eSCL ScannerCapabilities endpoint."""
        url = f"{scanner.base_url}/eSCL/ScannerCapabilities"
        try:
            req = Request(url, method="GET")
            with urlopen(req, timeout=5) as resp:
                xml_data = resp.read()

            root = ET.fromstring(xml_data)
            caps = ScannerCapabilities()

            # Parse sources
            sources = []
            if root.find(f".//{{{SCAN_NS_URI}}}Platen") is not None:
                sources.append(ScanSource.FLATBED)
            if root.find(f".//{{{SCAN_NS_URI}}}Adf") is not None:
                sources.append(ScanSource.ADF)
            caps.sources = sources or [ScanSource.FLATBED]

            # Parse resolutions from Platen (or Adf)
            res_els = root.findall(
                f".//{{{SCAN_NS_URI}}}DiscreteResolution/{{{SCAN_NS_URI}}}XResolution"
            )
            if res_els:
                resolutions = sorted(set(
                    int(r.text) for r in res_els if r.text and r.text.isdigit()
                ))
                if resolutions:
                    caps.resolutions = resolutions

            # Parse document formats
            format_els = root.findall(
                f".//{{{SCAN_NS_URI}}}DocumentFormatExt"
            )
            if format_els:
                caps.formats = [f.text for f in format_els if f.text]

            # Parse color modes
            color_els = root.findall(
                f".//{{{SCAN_NS_URI}}}ColorMode"
            )
            if color_els:
                caps.color_modes = list(set(c.text for c in color_els if c.text))

            # Check duplex
            duplex_el = root.find(f".//{{{SCAN_NS_URI}}}AdfDuplexInputCaps")
            caps.duplex = duplex_el is not None

            return caps

        except (URLError, OSError, ET.ParseError) as e:
            logger.warning("Cannot get capabilities from %s: %s", scanner.ip, e)
            return ScannerCapabilities()

    def scan(
        self,
        scanner: ScannerInfo,
        settings: Optional[ScanSettings] = None,
    ) -> bytes:
        """Execute a scan via eSCL and return PDF bytes."""
        if settings is None:
            settings = ScanSettings()

        # Clean up any stale jobs first
        self._cleanup_stale_jobs(scanner)

        # Build scan request XML
        scan_xml = self._build_scan_request(settings)

        # Create scan job
        job_url = self._create_scan_job(scanner, scan_xml)
        logger.info("Scan job created: %s", job_url)

        # Wait briefly for scanner to process
        time.sleep(1.0)

        # Download the scan result
        pdf_data = self._download_scan(job_url)
        logger.info("Scan complete: %d bytes", len(pdf_data))

        return pdf_data

    def _build_scan_request(self, settings: ScanSettings) -> bytes:
        """Build the eSCL ScanSettings XML."""
        # Use Feeder (not Adf!) for ADF source - critical for HP compatibility
        input_source = settings.source.value

        xml = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="{SCAN_NS_URI}"
    xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
    <pwg:Version>2.0</pwg:Version>
    <pwg:ScanRegions>
        <pwg:ScanRegion>
            <pwg:Height>3300</pwg:Height>
            <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
            <pwg:Width>2550</pwg:Width>
            <pwg:XOffset>0</pwg:XOffset>
            <pwg:YOffset>0</pwg:YOffset>
        </pwg:ScanRegion>
    </pwg:ScanRegions>
    <scan:InputSource>{input_source}</scan:InputSource>
    <scan:ColorMode>{settings.color_mode}</scan:ColorMode>
    <scan:XResolution>{settings.resolution}</scan:XResolution>
    <scan:YResolution>{settings.resolution}</scan:YResolution>
    <pwg:DocumentFormat>{settings.format}</pwg:DocumentFormat>
    <scan:Intent>Document</scan:Intent>
</scan:ScanSettings>"""
        return xml.encode("utf-8")

    def _create_scan_job(self, scanner: ScannerInfo, scan_xml: bytes) -> str:
        """POST scan job and return the job URL from Location header."""
        url = f"{scanner.base_url}/eSCL/ScanJobs"
        req = Request(
            url,
            data=scan_xml,
            method="POST",
            headers={"Content-Type": "text/xml"},
        )

        max_retries = 3
        for attempt in range(max_retries):
            try:
                with urlopen(req, timeout=30) as resp:
                    location = resp.headers.get("Location", "")
                    if location:
                        if not location.startswith("http"):
                            location = f"{scanner.base_url}{location}"
                        return location
                    raise RuntimeError("No Location header in scan job response")

            except HTTPError as e:
                if e.code == 409 and attempt < max_retries - 1:
                    logger.warning("Scanner busy (409), cleaning up and retrying...")
                    self._cleanup_stale_jobs(scanner)
                    time.sleep(2)
                    continue
                raise RuntimeError(f"Failed to create scan job: HTTP {e.code}") from e

        raise RuntimeError("Failed to create scan job after retries")

    def _download_scan(self, job_url: str) -> bytes:
        """Download the scan result from the job URL."""
        download_url = f"{job_url}/NextDocument"

        max_wait = 60
        poll_interval = 1.0
        waited = 0.0

        while waited < max_wait:
            try:
                req = Request(download_url, method="GET")
                with urlopen(req, timeout=30) as resp:
                    return resp.read()

            except HTTPError as e:
                if e.code == 503:
                    # Scanner still processing
                    time.sleep(poll_interval)
                    waited += poll_interval
                    continue
                raise RuntimeError(f"Failed to download scan: HTTP {e.code}") from e

        raise RuntimeError("Scan download timed out")

    def _cleanup_stale_jobs(self, scanner: ScannerInfo) -> None:
        """Try to cancel any stale scan jobs."""
        status = self.get_status(scanner)
        if status.state == ScannerState.PROCESSING:
            logger.info("Scanner is processing, waiting for it to finish...")
            for _ in range(10):
                time.sleep(1)
                status = self.get_status(scanner)
                if status.state != ScannerState.PROCESSING:
                    break

    def test_connection(self, scanner: ScannerInfo) -> bool:
        """Test if a scanner is reachable via eSCL."""
        try:
            status = self.get_status(scanner)
            return status.state != ScannerState.OFFLINE
        except Exception:
            return False
