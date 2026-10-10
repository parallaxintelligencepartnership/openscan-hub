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

from .base import (
    to_protocol_color_mode,
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

# WSD Format tokens (not MIME types) in order of preference.
_FORMAT_PREFERENCE = ["pdf-a", "png", "tiff", "jfif", "jpeg", "exif", "dib"]
_DEFAULT_FORMAT = "jfif"


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

            # Parse resolutions (scoped to the actual resolution-list
            # elements - a bare ".//Width" search also matches unrelated
            # Width elements like PlatenMinimumSize/PlatenMaximumSize)
            res_els = (
                root.findall(".//wscn:PlatenResolutions/wscn:Widths/wscn:Width", NS)
                + root.findall(".//wscn:ADFResolutions/wscn:Widths/wscn:Width", NS)
            )
            if res_els:
                resolutions = sorted(set(
                    int(r.text) for r in res_els if r.text and r.text.isdigit()
                ))
                if resolutions:
                    caps.resolutions = resolutions

            # Parse supported WSD format tokens (not MIME types)
            format_els = root.findall(".//wscn:FormatsSupported/wscn:FormatValue", NS)
            if format_els:
                caps.formats = [f.text.strip() for f in format_els if f.text]

            # Native max scan area (WSD scan units, ~1/1000 inch) - used to
            # build an explicit InputMediaSize, since many devices (e.g.
            # Canon G-series) advertise DocumentSizeAutoDetectSupported=false
            # and reject CreateScanJob requests that ask for auto-detect.
            max_size_el = root.find(".//wscn:PlatenMaximumSize", NS)
            if max_size_el is not None:
                w_el = max_size_el.find("wscn:Width", NS)
                h_el = max_size_el.find("wscn:Height", NS)
                if w_el is not None and w_el.text and w_el.text.isdigit():
                    caps.max_width = int(w_el.text)
                if h_el is not None and h_el.text and h_el.text.isdigit():
                    caps.max_height = int(h_el.text)

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

        self._wait_until_idle(scanner)

        caps = self.get_capabilities(scanner)
        format_token = self._pick_format(caps.formats)

        # Create scan job
        job_xml = self._build_create_scan_job(
            settings, format_token, caps.max_width, caps.max_height
        )
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

        # Retrieve image. WSD returns this as an MTOM/XOP multipart
        # response (a SOAP envelope part plus a separate binary image
        # part), not a bare image body - it must be demultiplexed.
        retrieve_xml = self._build_retrieve_image(job_id, job_token)
        raw_data, content_type = self._soap_request(
            scanner,
            "http://schemas.microsoft.com/windows/2006/08/wdp/scan/RetrieveImage",
            retrieve_xml,
            raw_response=True,
        )
        image_data = self._extract_mtom_part(raw_data, content_type)

        logger.info("WSD scan complete: %d bytes", len(image_data))
        return image_data

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
            with urlopen(req, timeout=30) as resp:
                data = resp.read()
                if raw_response:
                    return data, resp.headers.get("Content-Type", "")
                return data.decode("utf-8")
        except HTTPError as e:
            fault = self._parse_soap_fault(e)
            logger.error(
                "WSD %s failed for %s: HTTP %d%s",
                action, url, e.code, f" - {fault}" if fault else "",
            )
            detail = f"HTTP {e.code}" + (f" ({fault})" if fault else "")
            raise RuntimeError(f"WSD SOAP request failed: {detail}") from e

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
            parts.append(subcode_el.text.strip())
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
        available = {f.lower() for f in formats_supported}
        for candidate in _FORMAT_PREFERENCE:
            if candidate in available:
                return candidate
        if formats_supported:
            return formats_supported[0]
        return _DEFAULT_FORMAT

    def _build_create_scan_job(
        self,
        settings: ScanSettings,
        format_token: str,
        max_width: int = 0,
        max_height: int = 0,
    ) -> str:
        # Element order/structure follows sane-airscan's wsd_scan_query()
        # (a WSD client verified working against real-world devices,
        # including this exact Canon G-series model) rather than guessing
        # from the abstract (alphabetically-listed) MS docs - a plain
        # ScanTicket built from the docs alone got InvalidArgs from this
        # printer. Notably: no top-level Scaling element, ImagesToTransfer
        # is required, and the scan area is set via ScanRegion nested
        # inside MediaFront/MediaBack, not Scaling. JobInformation is
        # spec-optional but sane-airscan notes Canon devices reject the
        # job with the same InvalidArgs fault without it.
        input_source = "ADF" if settings.source == ScanSource.ADF else "Platen"
        color_mode = to_protocol_color_mode(settings.color_mode)

        # Many devices (e.g. Canon G-series) advertise
        # DocumentSizeAutoDetectSupported=false and reject a job that asks
        # for auto-detect; fall back to the device's own reported max size.
        if max_width and max_height:
            input_size = f"""<wscn:InputMediaSize>
                            <wscn:Width>{max_width}</wscn:Width>
                            <wscn:Height>{max_height}</wscn:Height>
                        </wscn:InputMediaSize>"""
            scan_region = f"""<wscn:ScanRegion>
                                <wscn:ScanRegionXOffset>0</wscn:ScanRegionXOffset>
                                <wscn:ScanRegionYOffset>0</wscn:ScanRegionYOffset>
                                <wscn:ScanRegionWidth>{max_width}</wscn:ScanRegionWidth>
                                <wscn:ScanRegionHeight>{max_height}</wscn:ScanRegionHeight>
                            </wscn:ScanRegion>"""
        else:
            input_size = "<wscn:DocumentSizeAutoDetect>true</wscn:DocumentSizeAutoDetect>"
            scan_region = ""

        return f"""<wscn:CreateScanJobRequest>
            <wscn:ScanTicket>
                <wscn:JobDescription>
                    <wscn:JobName>OpenScanHub Scan</wscn:JobName>
                    <wscn:JobOriginatingUserName>OpenScanHub</wscn:JobOriginatingUserName>
                    <wscn:JobInformation>OpenScanHub</wscn:JobInformation>
                </wscn:JobDescription>
                <wscn:DocumentParameters>
                    <wscn:Format>{format_token}</wscn:Format>
                    <wscn:ImagesToTransfer>1</wscn:ImagesToTransfer>
                    <wscn:InputSize>
                        {input_size}
                    </wscn:InputSize>
                    <wscn:InputSource>{input_source}</wscn:InputSource>
                    <wscn:MediaSides>
                        <wscn:MediaFront>
                            <wscn:ColorProcessing>{color_mode}</wscn:ColorProcessing>
                            <wscn:Resolution>
                                <wscn:Width>{settings.resolution}</wscn:Width>
                                <wscn:Height>{settings.resolution}</wscn:Height>
                            </wscn:Resolution>
                            {scan_region}
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
