"""WSD protocol tests from recorded-style fixtures and a fake SOAP device."""

import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from openscan.scanner.base import ScannerCapabilities, ScannerInfo, ScanSettings, ScanSource
from openscan.scanner.wsd import WsdScanner, build_probe, parse_capabilities

WSCN = "http://schemas.microsoft.com/windows/2006/08/wdp/scan"
SOAP = "http://www.w3.org/2003/05/soap-envelope"
VALID_FORMATS = {"dib", "exif", "jbig", "jfif", "jpeg2k", "pdf-a", "png", "tiff-single-uncompressed",
                 "tiff-single-g4", "tiff-single-g3mh", "tiff-single-jpeg-tn2", "tiff-multi-uncompressed",
                 "tiff-multi-g4", "xps"}

PROBE_MATCH = b"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="http://www.w3.org/2003/05/soap-envelope"
  xmlns:wsa="http://schemas.xmlsoap.org/ws/2004/08/addressing"
  xmlns:wsd="http://schemas.xmlsoap.org/ws/2005/04/discovery"
  xmlns:wscn="http://schemas.microsoft.com/windows/2006/08/wdp/scan">
 <soap:Header><wsa:Action>http://schemas.xmlsoap.org/ws/2005/04/discovery/ProbeMatches</wsa:Action></soap:Header>
 <soap:Body><wsd:ProbeMatches><wsd:ProbeMatch>
  <wsa:EndpointReference><wsa:Address>urn:uuid:16a65700-007c-1000-bb49-001122334455</wsa:Address></wsa:EndpointReference>
  <wsd:Types>wscn:ScanDeviceType</wsd:Types>
  <wsd:XAddrs>http://192.168.1.20:5357/16a65700-007c-1000-bb49-001122334455/ http://[fe80::1]:5357/x</wsd:XAddrs>
  <wsd:MetadataVersion>1</wsd:MetadataVersion>
 </wsd:ProbeMatch></wsd:ProbeMatches></soap:Body>
</soap:Envelope>"""

CAPS = f"""<?xml version="1.0" encoding="utf-8"?>
<soap:Envelope xmlns:soap="{SOAP}" xmlns:wscn="{WSCN}"><soap:Body>
<wscn:GetScannerElementsResponse><wscn:ScannerElements>
 <wscn:ElementData Name="wscn:ScannerConfiguration" Valid="true"><wscn:ScannerConfiguration>
  <wscn:DeviceSettings>
   <wscn:FormatsSupported><wscn:FormatValue>jfif</wscn:FormatValue><wscn:FormatValue>pdf-a</wscn:FormatValue></wscn:FormatsSupported>
   <wscn:DocumentSizeAutoDetectSupported>false</wscn:DocumentSizeAutoDetectSupported>
  </wscn:DeviceSettings>
  <wscn:Platen>
   <wscn:PlatenColor><wscn:ColorEntry>RGB24</wscn:ColorEntry><wscn:ColorEntry>Grayscale8</wscn:ColorEntry></wscn:PlatenColor>
   <wscn:PlatenMinimumSize><wscn:Width>100</wscn:Width><wscn:Height>100</wscn:Height></wscn:PlatenMinimumSize>
   <wscn:PlatenMaximumSize><wscn:Width>8500</wscn:Width><wscn:Height>11693</wscn:Height></wscn:PlatenMaximumSize>
   <wscn:PlatenResolutions><wscn:Widths><wscn:Width>300</wscn:Width><wscn:Width>600</wscn:Width></wscn:Widths>
    <wscn:Heights><wscn:Height>300</wscn:Height><wscn:Height>600</wscn:Height></wscn:Heights></wscn:PlatenResolutions>
  </wscn:Platen>
  <wscn:ADF>
   <wscn:ADFSupportsDuplex>true</wscn:ADFSupportsDuplex>
   <wscn:ADFFront>
    <wscn:ADFColor><wscn:ColorEntry>RGB24</wscn:ColorEntry><wscn:ColorEntry>BlackAndWhite1</wscn:ColorEntry></wscn:ADFColor>
    <wscn:ADFMaximumSize><wscn:Width>8500</wscn:Width><wscn:Height>14000</wscn:Height></wscn:ADFMaximumSize>
    <wscn:ADFResolutions><wscn:Widths><wscn:Width>200</wscn:Width><wscn:Width>300</wscn:Width></wscn:Widths></wscn:ADFResolutions>
   </wscn:ADFFront>
  </wscn:ADF>
 </wscn:ScannerConfiguration></wscn:ElementData>
 <wscn:ElementData Name="wscn:ScannerStatus" Valid="true"><wscn:ScannerStatus>
  <wscn:ScannerState>Idle</wscn:ScannerState></wscn:ScannerStatus></wscn:ElementData>
</wscn:ScannerElements></wscn:GetScannerElementsResponse></soap:Body></soap:Envelope>"""

BOUNDARY = "uuid:0a1b2c3d"
MTOM_CTYPE = (f'multipart/related; type="application/xop+xml"; boundary="{BOUNDARY}"; '
              'start="<root>"; start-info="application/soap+xml"')


def _pdf_bytes():
    img = Image.new("RGB", (200, 300), "white")
    ImageDraw.Draw(img).rectangle((20, 20, 180, 280), fill="black")
    out = BytesIO()
    img.save(out, format="PDF")
    return out.getvalue()


def _mtom(payload: bytes) -> bytes:
    env = (f'<soap:Envelope xmlns:soap="{SOAP}" xmlns:wscn="{WSCN}"><soap:Body><wscn:RetrieveImageResponse>'
           '<wscn:ScanData><xop:Include xmlns:xop="http://www.w3.org/2004/08/xop/include" href="cid:img"/>'
           '</wscn:ScanData></wscn:RetrieveImageResponse></soap:Body></soap:Envelope>').encode()
    return (f"--{BOUNDARY}\r\nContent-Type: application/xop+xml; charset=UTF-8; type=\"application/soap+xml\"\r\n"
            "Content-ID: <root>\r\n\r\n").encode() + env + \
        (f"\r\n--{BOUNDARY}\r\nContent-Type: application/octet-stream\r\nContent-Transfer-Encoding: binary\r\n"
         "Content-ID: <img>\r\n\r\n").encode() + payload + f"\r\n--{BOUNDARY}--\r\n".encode()


def _fault(subcode):
    return (f'<soap:Envelope xmlns:soap="{SOAP}" xmlns:wscn="{WSCN}"><soap:Body><soap:Fault>'
            '<soap:Code><soap:Value>soap:Sender</soap:Value><soap:Subcode>'
            f'<soap:Value>wscn:{subcode}</soap:Value></soap:Subcode></soap:Code>'
            '<soap:Reason><soap:Text xml:lang="en">no images</soap:Text></soap:Reason>'
            '</soap:Fault></soap:Body></soap:Envelope>').encode()


def _tags(xml):
    return {el.tag.split("}")[-1]: (el.text or "").strip() for el in ET.fromstring(xml).iter()}


def test_probe_is_namespace_well_formed():
    probe = build_probe("urn:uuid:1")
    root = ET.fromstring(probe)
    assert 'xmlns:wscn="http://schemas.microsoft.com/windows/2006/08/wdp/scan"' in probe
    assert root.find(".//{http://schemas.xmlsoap.org/ws/2005/04/discovery}Types").text == "wscn:ScanDeviceType"


def test_probe_match_with_xaddrs_path(monkeypatch):
    seen = {}

    def fake_resolve(self, url, epr):
        seen["args"] = (url, epr)
        return None

    monkeypatch.setattr(WsdScanner, "_resolve_scan_service", fake_resolve)
    info = WsdScanner()._parse_probe_response(PROBE_MATCH, "192.168.1.20")
    assert (info.ip, info.port) == ("192.168.1.20", 5357)
    assert info.uuid == "urn:uuid:16a65700-007c-1000-bb49-001122334455"
    assert seen["args"] == ("http://192.168.1.20:5357/16a65700-007c-1000-bb49-001122334455/", info.uuid)

    monkeypatch.setattr(WsdScanner, "_resolve_scan_service",
                        lambda self, u, e: ("192.168.1.20", 80, "/wsd/scanservice.cgi", "Canon G3010"))
    info = WsdScanner()._parse_probe_response(PROBE_MATCH, "192.168.1.20")
    assert (info.port, info.path, info.model) == (80, "/wsd/scanservice.cgi", "Canon G3010")


def test_capabilities_scoped():
    caps = parse_capabilities(ET.fromstring(CAPS))
    assert caps.resolutions == [200, 300, 600]  # no 8500 from PlatenMaximumSize
    assert caps.duplex is True
    assert caps.formats == ["jfif", "pdf-a"]
    assert caps.source_sizes == {"Platen": (8500, 11693), "Feeder": (8500, 14000)}
    assert caps.auto_detect is False
    assert set(caps.color_modes) == {"RGB24", "Grayscale8", "BlackAndWhite1"}


def test_create_scan_job_ticket():
    s = WsdScanner()
    caps = parse_capabilities(ET.fromstring(CAPS))
    token = s._pick_format(caps.formats)
    assert token == "pdf-a"
    xml = s._build_create_scan_job(
        ScanSettings(source=ScanSource.ADF, page_size="a4", color_mode="bw", resolution=250, duplex=True),
        token, caps=caps)
    wrapped = f'<x xmlns:wscn="{WSCN}">{xml}</x>'
    t = _tags(wrapped)
    assert t["Format"] in VALID_FORMATS
    assert t["JobInformation"] and t["JobOriginatingUserName"]
    assert "DocumentSizeAutoDetect" not in t
    assert (t["ScanRegionWidth"], t["ScanRegionHeight"]) == ("8268", "11693")
    assert t["InputSource"] == "ADFDuplex" and "MediaBack" in t
    assert t["ImagesToTransfer"] == "0"
    assert t["ColorProcessing"] == "BlackAndWhite1"
    assert ET.fromstring(wrapped).find(f".//{{{WSCN}}}Resolution/{{{WSCN}}}Width").text == "300"


def test_ticket_without_caps_never_auto_detects():
    s = WsdScanner()
    xml = s._build_create_scan_job(ScanSettings(), s._pick_format([]), caps=ScannerCapabilities())
    t = _tags(f'<x xmlns:wscn="{WSCN}">{xml}</x>')
    assert t["Format"] == "jfif"
    assert "DocumentSizeAutoDetect" not in t
    assert (t["Width"], t["ImagesToTransfer"], t["InputSource"]) == ("300", "1", "Platen")
    assert t["ScanRegionWidth"] == "8500" and t["ScanRegionHeight"] == "11000"


def test_pick_format_ignores_unreadable_tokens():
    assert WsdScanner()._pick_format(["xps"]) == "jfif"
    assert WsdScanner()._pick_format(["PNG", "dib"]) == "png"


def test_mtom_extraction_returns_binary_part():
    pdf = _pdf_bytes()
    assert WsdScanner()._extract_mtom_part(_mtom(pdf), MTOM_CTYPE) == pdf


@pytest.fixture
def wsd_device():
    state = {"retrieve": [], "actions": [], "tickets": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/soap+xml"):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
            action = ET.fromstring(body).find(".//{http://schemas.xmlsoap.org/ws/2004/08/addressing}Action").text
            name = action.rsplit("/", 1)[-1]
            state["actions"].append(name)
            if name == "GetScannerElements":
                return self._send(200, CAPS.encode())
            if name == "CreateScanJob":
                state["tickets"].append(body)
                return self._send(200, (f'<soap:Envelope xmlns:soap="{SOAP}" xmlns:wscn="{WSCN}"><soap:Body>'
                                        '<wscn:CreateScanJobResponse><wscn:JobId>7</wscn:JobId>'
                                        '<wscn:JobToken>tok</wscn:JobToken></wscn:CreateScanJobResponse>'
                                        '</soap:Body></soap:Envelope>').encode())
            if name == "RetrieveImage":
                item = state["retrieve"].pop(0)
                if isinstance(item, str):
                    return self._send(400, _fault(item))
                return self._send(200, _mtom(item), MTOM_CTYPE)
            self._send(404, b"")

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    state["info"] = ScannerInfo(name="w", ip="127.0.0.1", port=srv.server_address[1],
                                protocol="wsd", path="/wsd/scan")
    yield state
    srv.shutdown()
    srv.server_close()


def _scanner():
    s = WsdScanner()
    s.retry_delay = 0.01
    s.http_timeout = s.retrieve_timeout = 5
    return s


def test_adf_scan_retrieves_until_no_images(wsd_device):
    wsd_device["retrieve"] = [_pdf_bytes(), _pdf_bytes(), _pdf_bytes(), "ClientErrorNoImagesAvailable"]
    pdf = _scanner().scan(wsd_device["info"], ScanSettings(source=ScanSource.ADF))
    assert len(PdfReader(BytesIO(pdf)).pages) == 3
    assert wsd_device["actions"].count("RetrieveImage") == 4


def test_flatbed_scan_retrieves_once(wsd_device):
    wsd_device["retrieve"] = [_pdf_bytes()]
    pdf = _scanner().scan(wsd_device["info"], ScanSettings())
    assert len(PdfReader(BytesIO(pdf)).pages) == 1
    assert wsd_device["actions"].count("RetrieveImage") == 1


def test_no_pages_is_a_clear_error(wsd_device):
    wsd_device["retrieve"] = ["ClientErrorNoImagesAvailable"] * 10
    with pytest.raises(RuntimeError, match="no pages"):
        _scanner().scan(wsd_device["info"], ScanSettings(source=ScanSource.ADF))
