"""WSD (Web Services for Devices) scanner protocol implementation.

WSD/WS-Scan is used by enterprise scanners (Xerox, Ricoh, etc.)
and many Windows-friendly devices. Uses SOAP over HTTP.
"""

import logging
import socket
import struct
import time
import uuid
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

# WSD XML Namespaces
NS = {
    "s": "http://www.w3.org/2003/05/soap-envelope",
    "a": "http://schemas.xmlsoap.org/ws/2004/08/addressing",
    "d": "http://schemas.xmlsoap.org/ws/2005/04/discovery",
    "wscn": "http://schemas.microsoft.com/windows/2006/08/wdp/scan",
    "wsd": "http://schemas.xmlsoap.org/ws/2006/02/devprof",
}

WSD_MULTICAST = "239.255.255.250"
WSD_PORT = 3702
WSD_SCAN_TYPE = "http://schemas.microsoft.com/windows/2006/08/wdp/scan:ScanDeviceType"


class WsdScanner(ScannerProtocol):
    """WSD (Web Services for Devices) scanner protocol."""

    def discover(self, timeout: float = 5.0) -> list[ScannerInfo]:
        """Discover WSD scanners via WS-Discovery multicast probe."""
        scanners = []
        message_id = f"urn:uuid:{uuid.uuid4()}"

        probe_xml = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
    xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery">
    <s:Header>
        <a:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</a:Action>
        <a:MessageID>{message_id}</a:MessageID>
        <a:To>urn:schemas-xmlsoap-org:ws:2005:04:discovery</a:To>
    </s:Header>
    <s:Body>
        <d:Probe>
            <d:Types>wscn:ScanDeviceType</d:Types>
        </d:Probe>
    </s:Body>
</s:Envelope>"""

        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.settimeout(timeout)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)

            # Send probe
            sock.sendto(probe_xml.encode("utf-8"), (WSD_MULTICAST, WSD_PORT))

            # Collect responses
            end_time = time.time() + timeout
            while time.time() < end_time:
                try:
                    data, addr = sock.recvfrom(65535)
                    scanner = self._parse_probe_response(data, addr[0])
                    if scanner:
                        scanners.append(scanner)
                except socket.timeout:
                    break

            sock.close()

        except OSError as e:
            logger.warning("WSD discovery failed: %s", e)

        return scanners

    def get_status(self, scanner: ScannerInfo) -> ScannerStatus:
        """Get scanner status via WSD GetScannerElements."""
        try:
            resp_xml = self._soap_request(
                scanner,
                "http://schemas.microsoft.com/windows/2006/08/wdp/scan/GetScannerElements",
                self._build_get_scanner_elements(),
            )
            root = ET.fromstring(resp_xml)

            state_el = root.find(".//wscn:ScannerState", NS)
            state = ScannerState.IDLE
            if state_el is not None:
                state_map = {
                    "Idle": ScannerState.IDLE,
                    "Processing": ScannerState.PROCESSING,
                    "Stopped": ScannerState.STOPPED,
                }
                state = state_map.get(state_el.text, ScannerState.IDLE)

            adf_el = root.find(".//wscn:AdfState", NS)
            adf_state = AdfState.UNKNOWN
            if adf_el is not None:
                adf_map = {
                    "Empty": AdfState.EMPTY,
                    "Loaded": AdfState.LOADED,
                    "Jammed": AdfState.JAMMED,
                }
                adf_state = adf_map.get(adf_el.text, AdfState.UNKNOWN)

            return ScannerStatus(state=state, adf_state=adf_state)

        except Exception as e:
            logger.debug("WSD status failed for %s: %s", scanner.ip, e)
            return ScannerStatus(state=ScannerState.OFFLINE)

    def get_capabilities(self, scanner: ScannerInfo) -> ScannerCapabilities:
        """Get scanner capabilities via WSD GetScannerElements."""
        caps = ScannerCapabilities()

        try:
            resp_xml = self._soap_request(
                scanner,
                "http://schemas.microsoft.com/windows/2006/08/wdp/scan/GetScannerElements",
                self._build_get_scanner_elements(),
            )
            root = ET.fromstring(resp_xml)

            # Parse sources
            sources = []
            if root.find(".//wscn:Platen", NS) is not None:
                sources.append(ScanSource.FLATBED)
            if root.find(".//wscn:ADF", NS) is not None:
                sources.append(ScanSource.ADF)
            caps.sources = sources or [ScanSource.FLATBED]

            # Parse resolutions
            res_els = root.findall(".//wscn:Width", NS)
            if res_els:
                resolutions = sorted(set(
                    int(r.text) for r in res_els if r.text and r.text.isdigit()
                ))
                if resolutions:
                    caps.resolutions = resolutions

            # Duplex
            duplex_el = root.find(".//wscn:AdfDuplexer", NS)
            caps.duplex = duplex_el is not None

        except Exception as e:
            logger.debug("WSD capabilities failed for %s: %s", scanner.ip, e)

        return caps

    def scan(
        self,
        scanner: ScannerInfo,
        settings: Optional[ScanSettings] = None,
    ) -> bytes:
        """Execute a scan via WSD CreateScanJob + RetrieveImage."""
        if settings is None:
            settings = ScanSettings()

        # Create scan job
        job_xml = self._build_create_scan_job(settings)
        resp_xml = self._soap_request(
            scanner,
            "http://schemas.microsoft.com/windows/2006/08/wdp/scan/CreateScanJob",
            job_xml,
        )

        root = ET.fromstring(resp_xml)
        job_id_el = root.find(".//wscn:JobId", NS)
        job_token_el = root.find(".//wscn:JobToken", NS)

        if job_id_el is None or job_token_el is None:
            raise RuntimeError("Failed to create WSD scan job - no JobId/JobToken")

        job_id = job_id_el.text
        job_token = job_token_el.text
        logger.info("WSD scan job created: %s", job_id)

        # Wait briefly
        time.sleep(1.0)

        # Retrieve image
        retrieve_xml = self._build_retrieve_image(job_id, job_token)
        image_data = self._soap_request(
            scanner,
            "http://schemas.microsoft.com/windows/2006/08/wdp/scan/RetrieveImage",
            retrieve_xml,
            raw_response=True,
        )

        logger.info("WSD scan complete: %d bytes", len(image_data))
        return image_data

    def test_connection(self, scanner: ScannerInfo) -> bool:
        """Test if a WSD scanner is reachable."""
        try:
            status = self.get_status(scanner)
            return status.state != ScannerState.OFFLINE
        except Exception:
            return False

    # --- Internal methods ---

    def _parse_probe_response(self, data: bytes, ip: str) -> Optional[ScannerInfo]:
        """Parse a WS-Discovery ProbeMatch response."""
        try:
            root = ET.fromstring(data)
            types_el = root.find(".//d:Types", NS)

            # Check if this is a scan device
            if types_el is None or "ScanDeviceType" not in (types_el.text or ""):
                return None

            xaddrs_el = root.find(".//d:XAddrs", NS)
            if xaddrs_el is None or not xaddrs_el.text:
                return None

            # Parse the first XAddr URL for port
            xaddr = xaddrs_el.text.split()[0]
            port = 80
            if ":" in xaddr.split("//")[-1]:
                try:
                    port = int(xaddr.split(":")[-1].rstrip("/"))
                except ValueError:
                    pass

            return ScannerInfo(
                name=f"WSD Scanner at {ip}",
                ip=ip,
                port=port,
                protocol="wsd",
                model="WSD Scanner",
            )

        except ET.ParseError:
            return None

    def _soap_request(
        self,
        scanner: ScannerInfo,
        action: str,
        body_xml: str,
        raw_response: bool = False,
    ):
        """Send a SOAP request to the scanner."""
        message_id = f"urn:uuid:{uuid.uuid4()}"
        url = f"http://{scanner.ip}:{scanner.port}/wsd/scan"

        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
    xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:wscn="http://schemas.microsoft.com/windows/2006/08/wdp/scan">
    <s:Header>
        <a:Action>{action}</a:Action>
        <a:MessageID>{message_id}</a:MessageID>
        <a:To>urn:uuid:{scanner.uuid or uuid.uuid4()}</a:To>
    </s:Header>
    <s:Body>
        {body_xml}
    </s:Body>
</s:Envelope>"""

        req = Request(
            url,
            data=envelope.encode("utf-8"),
            method="POST",
            headers={
                "Content-Type": "application/soap+xml; charset=utf-8",
            },
        )

        try:
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
                return data if raw_response else data.decode("utf-8")
        except HTTPError as e:
            raise RuntimeError(f"WSD SOAP request failed: HTTP {e.code}") from e

    def _build_get_scanner_elements(self) -> str:
        return """<wscn:GetScannerElementsRequest>
            <wscn:RequestedElements>
                <wscn:Name>wscn:ScannerDescription</wscn:Name>
                <wscn:Name>wscn:ScannerConfiguration</wscn:Name>
                <wscn:Name>wscn:ScannerStatus</wscn:Name>
            </wscn:RequestedElements>
        </wscn:GetScannerElementsRequest>"""

    def _build_create_scan_job(self, settings: ScanSettings) -> str:
        input_source = "ADF" if settings.source == ScanSource.ADF else "Platen"
        color_mode = "RGB24"
        if settings.color_mode == "Grayscale8":
            color_mode = "Grayscale8"

        return f"""<wscn:CreateScanJobRequest>
            <wscn:ScanTicket>
                <wscn:JobDescription>
                    <wscn:JobName>OpenScanHub Scan</wscn:JobName>
                </wscn:JobDescription>
                <wscn:DocumentParameters>
                    <wscn:Format>application/pdf</wscn:Format>
                    <wscn:InputSource>{input_source}</wscn:InputSource>
                    <wscn:InputSize>
                        <wscn:DocumentSizeAutoDetect>true</wscn:DocumentSizeAutoDetect>
                    </wscn:InputSize>
                    <wscn:Scaling>
                        <wscn:ScalingWidth>100</wscn:ScalingWidth>
                        <wscn:ScalingHeight>100</wscn:ScalingHeight>
                    </wscn:Scaling>
                    <wscn:MediaSides>
                        <wscn:MediaFront>
                            <wscn:ColorProcessing>{color_mode}</wscn:ColorProcessing>
                            <wscn:Resolution>
                                <wscn:Width>{settings.resolution}</wscn:Width>
                                <wscn:Height>{settings.resolution}</wscn:Height>
                            </wscn:Resolution>
                        </wscn:MediaFront>
                    </wscn:MediaSides>
                </wscn:DocumentParameters>
            </wscn:ScanTicket>
        </wscn:CreateScanJobRequest>"""

    def _build_retrieve_image(self, job_id: str, job_token: str) -> str:
        return f"""<wscn:RetrieveImageRequest>
            <wscn:JobId>{job_id}</wscn:JobId>
            <wscn:JobToken>{job_token}</wscn:JobToken>
            <wscn:DocumentDescription>
                <wscn:DocumentName>scan.pdf</wscn:DocumentName>
            </wscn:DocumentDescription>
        </wscn:RetrieveImageRequest>"""
