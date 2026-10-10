"""WSD (Web Services for Devices) scanner protocol implementation.

WSD/WS-Scan is used by enterprise scanners (Xerox, Ricoh, etc.)
and many Windows-friendly devices. Uses SOAP over HTTP.
"""

import logging
import os
import socket
import struct
import time
import uuid
import xml.etree.ElementTree as ET
from typing import Optional
from urllib.error import HTTPError, URLError
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

SCAN_ACTION = "http://schemas.microsoft.com/windows/2006/08/wdp/scan/"

# WS-Scan Format tokens (not MIME types) we can turn into a PDF, in order
# of preference.
_FORMAT_PREFERENCE = [
    "pdf-a", "jfif", "exif", "png",
    "tiff-single-uncompressed", "tiff-single-g4", "tiff-single-g3mh",
    "tiff-single-jpeg-tn2", "tiff-multi-uncompressed", "tiff-multi-g4",
    "dib",
]
_DEFAULT_FORMAT = "jfif"
# Faults that mean "no more images in this job".
_END_OF_JOB_FAULTS = ("NoImagesAvailable", "JobIdNotFound", "JobNotFound")


class WsdFault(RuntimeError):
    """A SOAP fault from the device. `subcode` is e.g. ClientErrorNoImagesAvailable."""

    def __init__(self, message: str, subcode: str = ""):
        super().__init__(message)
        self.subcode = subcode


def build_probe(message_id: str) -> str:
    """WS-Discovery Probe for scan devices."""
    return f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
    xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
    xmlns:wscn="http://schemas.microsoft.com/windows/2006/08/wdp/scan">
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


class WsdScanner(ScannerProtocol):
    """WSD (Web Services for Devices) scanner protocol."""

    # Timing knobs (seconds). Tests shrink these.
    http_timeout = 30.0
    retrieve_timeout = 60.0  # RetrieveImage may block while a page is scanned
    retry_delay = 1.0
    first_image_attempts = 5
    max_pages = 500

    def discover(self, timeout: float = 5.0) -> list[ScannerInfo]:
        """Discover WSD scanners via WS-Discovery multicast probe."""
        scanners = []
        message_id = f"urn:uuid:{uuid.uuid4()}"

        probe_xml = build_probe(message_id)

        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
            sock.settimeout(timeout)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 4)

            # By default the kernel sends multicast out via whatever
            # interface holds the default route. On hosts with multiple
            # NICs (e.g. a k8s pod with a CNI eth0 plus a second NIC
            # bridged onto the LAN via Multus/macvlan) that's often the
            # wrong one - the probe never reaches the scanner's network.
            # OPENSCAN_DISCOVERY_IP pins both the outgoing multicast
            # interface and the socket's source address so replies come
            # back on the same interface.
            bind_ip = os.environ.get("OPENSCAN_DISCOVERY_IP", "")
            if bind_ip:
                sock.setsockopt(
                    socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(bind_ip)
                )
                sock.bind((bind_ip, 0))

            # Send probe
            sock.sendto(probe_xml.encode("utf-8"), (WSD_MULTICAST, WSD_PORT))

            # Collect responses until the deadline; devices often answer twice.
            end_time = time.monotonic() + timeout
            seen: set[str] = set()
            while True:
                remaining = end_time - time.monotonic()
                if remaining <= 0:
                    break
                sock.settimeout(remaining)
                try:
                    data, addr = sock.recvfrom(65535)
                except socket.timeout:
                    break
                if addr[0] in seen:
                    continue
                scanner = self._parse_probe_response(data, addr[0])
                if scanner:
                    seen.add(addr[0])
                    scanners.append(scanner)

        except OSError as e:
            logger.warning("WSD discovery failed: %s", e)
        finally:
            if sock is not None:
                sock.close()

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

            state_el = root.find(".//wscn:ScannerStatus/wscn:ScannerState", NS)
            if state_el is None:
                state_el = root.find(".//wscn:ScannerState", NS)
            state = ScannerState.IDLE
            if state_el is not None and state_el.text:
                state_map = {
                    "Idle": ScannerState.IDLE,
                    "Processing": ScannerState.PROCESSING,
                    "Stopped": ScannerState.STOPPED,
                }
                state = state_map.get(state_el.text.strip(), ScannerState.IDLE)

            adf_el = root.find(".//wscn:AdfState", NS)
            adf_state = AdfState.UNKNOWN
            if adf_el is not None:
                adf_map = {
                    "Empty": AdfState.EMPTY,
                    "Loaded": AdfState.LOADED,
                    "Jammed": AdfState.JAMMED,
                }
                adf_state = adf_map.get((adf_el.text or "").strip(), AdfState.UNKNOWN)

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
            caps = parse_capabilities(ET.fromstring(resp_xml))
        except Exception as e:
            logger.warning("WSD capabilities failed for %s: %s", scanner.ip, e)

        return caps

    def scan(
        self,
        scanner: ScannerInfo,
        settings: Optional[ScanSettings] = None,
    ) -> bytes:
        """Execute a scan via WSD CreateScanJob + RetrieveImage."""
        if settings is None:
            settings = ScanSettings()

        self._wait_until_idle(scanner)

        caps = self.get_capabilities(scanner)
        if caps.sources and settings.source not in caps.sources:
            if settings.source == ScanSource.ADF:
                raise RuntimeError("This scanner has no document feeder.")
            raise RuntimeError("This scanner has no flatbed glass.")
        format_token = self._pick_format(caps.formats)

        job_xml = self._build_create_scan_job(settings, format_token, caps=caps)
        resp_xml = self._soap_request(scanner, SCAN_ACTION + "CreateScanJob", job_xml)

        root = ET.fromstring(resp_xml)
        job_id_el = root.find(".//wscn:JobId", NS)
        job_token_el = root.find(".//wscn:JobToken", NS)
        if job_id_el is None or job_token_el is None:
            raise RuntimeError("Failed to create WSD scan job - no JobId/JobToken")

        job_id = (job_id_el.text or "").strip()
        job_token = (job_token_el.text or "").strip()
        logger.info("WSD scan job created: %s", job_id)

        images = self._retrieve_images(
            scanner, job_id, job_token, multi=settings.source == ScanSource.ADF
        )
        resolution = nearest_resolution(settings.resolution, caps.resolutions)
        pdf = finish_scan(combine_pages(images, resolution), resolution,
                          settings.remove_blank_pages)
        logger.info("WSD scan complete: %d image(s), %d bytes", len(images), len(pdf))
        return pdf

    def _retrieve_images(self, scanner: ScannerInfo, job_id: str, job_token: str,
                         multi: bool) -> list[bytes]:
        """Call RetrieveImage until the device reports no more images.

        The response is MTOM/XOP `multipart/related` (a SOAP envelope part
        plus a binary image part) and must be demultiplexed.
        """
        retrieve_xml = self._build_retrieve_image(job_id, job_token)
        images: list[bytes] = []
        early_faults = 0
        while len(images) < self.max_pages:
            try:
                raw_data, content_type = self._soap_request(
                    scanner, SCAN_ACTION + "RetrieveImage", retrieve_xml,
                    raw_response=True, timeout=self.retrieve_timeout,
                )
            except WsdFault as f:
                if any(k in f.subcode for k in _END_OF_JOB_FAULTS):
                    if images:
                        break
                    if "NoImagesAvailable" in f.subcode and early_faults + 1 < self.first_image_attempts:
                        # Some devices fault until the first page is ready.
                        early_faults += 1
                        time.sleep(self.retry_delay)
                        continue
                    raise RuntimeError(
                        "The scanner returned no pages. Check that paper is loaded."
                    ) from f
                raise
            images.append(self._extract_mtom_part(raw_data, content_type))
            if not multi:
                break
        return images

    def _extract_mtom_part(self, raw_data: bytes, content_type: str) -> bytes:
        """Pull the binary image payload out of an MTOM/XOP multipart body.

        RetrieveImage responses are `multipart/related` (per the
        Content-Type header): one part is the SOAP envelope
        (application/xop+xml or application/soap+xml), the other is the
        raw scanned image. If the response isn't actually multipart
        (some devices/situations return the image directly), the raw
        bytes are returned unchanged.
        """
        if "multipart" not in content_type.lower():
            return raw_data

        import email

        header = f"Content-Type: {content_type}\r\n\r\n".encode("utf-8")
        msg = email.message_from_bytes(header + raw_data)

        if not msg.is_multipart():
            return raw_data

        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype in ("application/xop+xml", "application/soap+xml", "text/xml", "multipart/related"):
                continue
            payload = part.get_payload(decode=True)
            if payload:
                return payload

        return raw_data

    def test_connection(self, scanner: ScannerInfo) -> bool:
        """Test if a WSD scanner is reachable."""
        try:
            status = self.get_status(scanner)
            return status.state != ScannerState.OFFLINE
        except Exception:
            return False

    def _wait_until_idle(self, scanner: ScannerInfo, max_wait: float = 10.0) -> None:
        """Wait briefly for a busy scanner to go idle before starting a job.

        Some devices (e.g. Canon G-series) only accept one scan job at a
        time and reject a new CreateScanJob outright ("NotAcceptingJobs")
        while a previous one is still marked as processing, instead of
        queuing it.
        """
        status = self.get_status(scanner)
        if status.state != ScannerState.PROCESSING:
            return

        logger.info("WSD scanner %s is busy, waiting for it to go idle...", scanner.ip)
        waited = 0.0
        while waited < max_wait:
            time.sleep(1.0)
            waited += 1.0
            status = self.get_status(scanner)
            if status.state != ScannerState.PROCESSING:
                return

        raise RuntimeError(
            "Scanner is busy and did not become idle. If it stays stuck, "
            "power-cycling the device may be required to reset it."
        )

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

            # First XAddr is the device metadata endpoint (WS-Transfer Get)
            metadata_url = xaddrs_el.text.split()[0]

            device_addr_el = root.find(".//a:EndpointReference/a:Address", NS)
            device_uuid = (
                device_addr_el.text.strip()
                if device_addr_el is not None and device_addr_el.text
                else None
            )

            scanner = ScannerInfo(
                name=f"WSD Scanner at {ip}",
                ip=ip,
                port=80,
                protocol="wsd",
                model="WSD Scanner",
                uuid=device_uuid or "",
            )

            from urllib.parse import urlparse
            parsed = urlparse(metadata_url)
            if parsed.hostname:
                scanner.ip = parsed.hostname
            if parsed.port:
                scanner.port = parsed.port

            resolved = self._resolve_scan_service(metadata_url, device_uuid)
            if resolved:
                res_ip, res_port, res_path, model = resolved
                scanner.ip = res_ip
                scanner.port = res_port
                scanner.path = res_path
                if model:
                    scanner.model = model

            return scanner

        except ET.ParseError:
            return None

    def _resolve_scan_service(
        self, metadata_url: str, device_uuid: Optional[str] = None
    ) -> Optional[tuple[str, int, str, str]]:
        """Query WS-Transfer Get on the device metadata endpoint to find the
        actual scan service address (host, port, path, model name).

        The WS-Discovery XAddrs only points at the device's metadata
        endpoint; the real scan service lives at whatever address is
        listed under the device's "Hosted" relationships, which varies
        per vendor/firmware and must be resolved dynamically. Per
        WS-Addressing, <a:To> here must be the device's own
        EndpointReference (a urn:uuid from the ProbeMatch), not the
        metadata endpoint's transport URL - devices reject the latter.
        """
        from urllib.parse import urlparse

        message_id = f"urn:uuid:{uuid.uuid4()}"
        to_addr = device_uuid or metadata_url
        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
    xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing">
    <s:Header>
        <a:Action>http://schemas.xmlsoap.org/ws/2004/09/transfer/Get</a:Action>
        <a:MessageID>{message_id}</a:MessageID>
        <a:To>{to_addr}</a:To>
        <a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>
    </s:Header>
    <s:Body/>
</s:Envelope>"""

        try:
            req = Request(
                metadata_url,
                data=envelope.encode("utf-8"),
                method="POST",
                headers={"Content-Type": "application/soap+xml; charset=utf-8"},
            )
            with urlopen(req, timeout=5) as resp:
                root = ET.fromstring(resp.read())
        except (HTTPError, URLError, OSError, ET.ParseError) as e:
            logger.debug("WSD metadata resolution failed for %s: %s", metadata_url, e)
            return None

        model_el = root.find(".//wsd:ThisModel/wsd:ModelName", NS)
        model = model_el.text.strip() if model_el is not None and model_el.text else ""

        for hosted in root.findall(".//wsd:Hosted", NS):
            types_el = hosted.find("wsd:Types", NS)
            if types_el is None or "ScannerServiceType" not in (types_el.text or ""):
                continue
            addr_el = hosted.find("a:EndpointReference/a:Address", NS)
            if addr_el is None or not addr_el.text:
                continue
            parsed = urlparse(addr_el.text.strip())
            if not parsed.hostname:
                continue
            port = parsed.port or 80
            return parsed.hostname, port, parsed.path or "/", model

        return None

    def _soap_request(
        self,
        scanner: ScannerInfo,
        action: str,
        body_xml: str,
        raw_response: bool = False,
        timeout: Optional[float] = None,
    ):
        """Send a SOAP request to the scanner."""
        message_id = f"urn:uuid:{uuid.uuid4()}"
        url = f"http://{scanner.ip}:{scanner.port}{scanner.path or '/wsd/scan'}"

        envelope = f"""<?xml version="1.0" encoding="utf-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope"
    xmlns:a="http://schemas.xmlsoap.org/ws/2004/08/addressing"
    xmlns:wscn="http://schemas.microsoft.com/windows/2006/08/wdp/scan">
    <s:Header>
        <a:Action>{action}</a:Action>
        <a:MessageID>{message_id}</a:MessageID>
        <a:To>{url}</a:To>
        <a:ReplyTo><a:Address>http://schemas.xmlsoap.org/ws/2004/08/addressing/role/anonymous</a:Address></a:ReplyTo>
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
            with urlopen(req, timeout=timeout or self.http_timeout) as resp:
                data = resp.read()
                if raw_response:
                    return data, resp.headers.get("Content-Type", "")
                return data.decode("utf-8")
        except HTTPError as e:
            fault = self._parse_soap_fault(e)
            subcode = fault.split(":")[0].strip() if fault else ""
            detail = f"HTTP {e.code}" + (f" ({fault})" if fault else "")
            if not any(k in subcode for k in _END_OF_JOB_FAULTS):
                logger.error("WSD %s failed for %s: %s", action.rsplit("/", 1)[-1], url, detail)
            raise WsdFault(f"WSD SOAP request failed: {detail}", subcode) from e

    def _parse_soap_fault(self, error: HTTPError) -> str:
        """Extract the SOAP fault subcode/reason from an error response body."""
        try:
            body = error.read()
        except Exception:
            return ""
        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return body.decode("utf-8", errors="replace")[:200]

        soap_ns = "http://www.w3.org/2003/05/soap-envelope"
        subcode_el = root.find(f".//{{{soap_ns}}}Subcode/{{{soap_ns}}}Value")
        reason_el = root.find(f".//{{{soap_ns}}}Reason/{{{soap_ns}}}Text")
        parts = []
        if subcode_el is not None and subcode_el.text:
            parts.append(subcode_el.text.strip().split(":")[-1])
        if reason_el is not None and reason_el.text:
            parts.append(reason_el.text.strip())
        return ": ".join(parts)

    def _build_get_scanner_elements(self) -> str:
        return """<wscn:GetScannerElementsRequest>
            <wscn:RequestedElements>
                <wscn:Name>wscn:ScannerDescription</wscn:Name>
                <wscn:Name>wscn:ScannerConfiguration</wscn:Name>
                <wscn:Name>wscn:ScannerStatus</wscn:Name>
            </wscn:RequestedElements>
        </wscn:GetScannerElementsRequest>"""

    def _pick_format(self, formats_supported: list[str]) -> str:
        """Pick a WSD Format token, preferring higher quality formats."""
        available = {f.strip().lower() for f in formats_supported}
        for candidate in _FORMAT_PREFERENCE:
            if candidate in available:
                return candidate
        if formats_supported:
            logger.warning(
                "Scanner formats %s are not ones OpenScanHub can read; asking for %s",
                formats_supported, _DEFAULT_FORMAT,
            )
        return _DEFAULT_FORMAT

    def _build_create_scan_job(
        self,
        settings: ScanSettings,
        format_token: str,
        max_width: int = 0,
        max_height: int = 0,
        caps: Optional[ScannerCapabilities] = None,
    ) -> str:
        # Element order/structure follows sane-airscan's wsd_scan_query()
        # (a WSD client verified working against real-world devices,
        # including the Canon G-series) rather than the alphabetically
        # listed MS docs: no top-level Scaling element, ImagesToTransfer is
        # required, and the scan area is a ScanRegion inside
        # MediaFront/MediaBack. JobInformation is spec-optional but Canon
        # devices reject the job with InvalidArgs without it.
        if caps is None:
            caps = ScannerCapabilities()
            if max_width and max_height:
                caps.max_width, caps.max_height = max_width, max_height
        adf = settings.source == ScanSource.ADF
        duplex = False
        if settings.duplex and adf:
            if caps.duplex:
                duplex = True
            else:
                logger.warning("Two-sided scanning is not supported by this scanner; scanning one side")
        input_source = ("ADFDuplex" if duplex else "ADF") if adf else "Platen"
        color_mode = pick_color_mode(settings.color_mode, caps.color_modes)
        resolution = nearest_resolution(settings.resolution, caps.resolutions)

        # WS-Scan sizes are in 1/1000 inch. AutoDetect is only requested
        # when the device says it supports it (Canon G-series rejects it).
        max_size = caps.max_size_for(settings.source)
        page_size = (settings.page_size or "auto").lower()
        if page_size == "auto" and not (max_size[0] and max_size[1]) and caps.auto_detect:
            input_size = "<wscn:DocumentSizeAutoDetect>true</wscn:DocumentSizeAutoDetect>"
            region = ""
        else:
            width, height = scan_region(page_size, 1000, max_size)
            input_size = f"""<wscn:InputMediaSize>
                            <wscn:Width>{width}</wscn:Width>
                            <wscn:Height>{height}</wscn:Height>
                        </wscn:InputMediaSize>"""
            region = f"""<wscn:ScanRegion>
                                <wscn:ScanRegionXOffset>0</wscn:ScanRegionXOffset>
                                <wscn:ScanRegionYOffset>0</wscn:ScanRegionYOffset>
                                <wscn:ScanRegionWidth>{width}</wscn:ScanRegionWidth>
                                <wscn:ScanRegionHeight>{height}</wscn:ScanRegionHeight>
                            </wscn:ScanRegion>"""

        def side(tag: str) -> str:
            return f"""<wscn:{tag}>
                            <wscn:ColorProcessing>{color_mode}</wscn:ColorProcessing>
                            <wscn:Resolution>
                                <wscn:Width>{resolution}</wscn:Width>
                                <wscn:Height>{resolution}</wscn:Height>
                            </wscn:Resolution>
                            {region}
                        </wscn:{tag}>"""

        sides = side("MediaFront") + (side("MediaBack") if duplex else "")
        images_to_transfer = 0 if adf else 1  # 0 = until the feeder is empty

        return f"""<wscn:CreateScanJobRequest>
            <wscn:ScanTicket>
                <wscn:JobDescription>
                    <wscn:JobName>OpenScanHub Scan</wscn:JobName>
                    <wscn:JobOriginatingUserName>OpenScanHub</wscn:JobOriginatingUserName>
                    <wscn:JobInformation>OpenScanHub</wscn:JobInformation>
                </wscn:JobDescription>
                <wscn:DocumentParameters>
                    <wscn:Format>{format_token}</wscn:Format>
                    <wscn:ImagesToTransfer>{images_to_transfer}</wscn:ImagesToTransfer>
                    <wscn:InputSize>
                        {input_size}
                    </wscn:InputSize>
                    <wscn:InputSource>{input_source}</wscn:InputSource>
                    <wscn:MediaSides>
                        {sides}
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


def _bool(text: Optional[str]) -> bool:
    return (text or "").strip().lower() in ("true", "1")


def _size(el: Optional[ET.Element]) -> tuple[int, int]:
    if el is None:
        return 0, 0
    w = (el.findtext("wscn:Width", "", NS) or "").strip()
    h = (el.findtext("wscn:Height", "", NS) or "").strip()
    return (int(w) if w.isdigit() else 0, int(h) if h.isdigit() else 0)


def parse_capabilities(root: ET.Element) -> ScannerCapabilities:
    """Parse a GetScannerElements response (ScannerConfiguration).

    Searches are scoped to the specific parent elements: a bare
    ".//Width" would also match PlatenMaximumSize (1/1000 inch values).
    """
    caps = ScannerCapabilities()
    config = root.find(".//wscn:ScannerConfiguration", NS)
    if config is None:
        config = root
    platen = config.find(".//wscn:Platen", NS)
    adf = config.find(".//wscn:ADF", NS)

    sources = []
    if platen is not None:
        sources.append(ScanSource.FLATBED)
    if adf is not None:
        sources.append(ScanSource.ADF)
    caps.sources = sources or [ScanSource.FLATBED]

    res_els = (
        config.findall(".//wscn:PlatenResolutions/wscn:Widths/wscn:Width", NS)
        + config.findall(".//wscn:ADFResolutions/wscn:Widths/wscn:Width", NS)
    )
    resolutions = sorted({int(r.text.strip()) for r in res_els
                          if r.text and r.text.strip().isdigit()})
    if resolutions:
        caps.resolutions = resolutions

    format_els = config.findall(".//wscn:FormatsSupported/wscn:FormatValue", NS)
    formats = [f.text.strip() for f in format_els if f.text and f.text.strip()]
    if formats:
        caps.formats = formats

    colors: list[str] = []
    for c in config.findall(".//wscn:PlatenColor/wscn:ColorEntry", NS) + \
            config.findall(".//wscn:ADFColor/wscn:ColorEntry", NS):
        if c.text and c.text.strip() not in colors:
            colors.append(c.text.strip())
    caps.color_modes = colors

    if platen is not None:
        size = _size(platen.find("wscn:PlatenMaximumSize", NS))
        if size[0] and size[1]:
            caps.source_sizes[ScanSource.FLATBED.value] = size
    if adf is not None:
        size = _size(adf.find(".//wscn:ADFMaximumSize", NS))
        if size[0] and size[1]:
            caps.source_sizes[ScanSource.ADF.value] = size
        caps.duplex = _bool(adf.findtext("wscn:ADFSupportsDuplex", "", NS))
    first = caps.source_sizes.get(caps.sources[0].value)
    if first:
        caps.max_width, caps.max_height = first
    caps.auto_detect = _bool(config.findtext(".//wscn:DocumentSizeAutoDetectSupported", "", NS))
    return caps
