"""eSCL protocol tests against a fake device served by http.server."""

import threading
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from openscan.scanner.base import ScannerInfo, ScanSettings, ScanSource, ScannerState
from openscan.scanner.escl import EsclScanner, parse_capabilities

NS = "http://schemas.hp.com/imaging/escl/2011/05/03"
PWG = "http://www.pwg.org/schemas/2010/12/sm"


def _input_caps(tag, max_w, max_h):
    return f"""<scan:{tag}>
      <scan:MaxWidth>{max_w}</scan:MaxWidth><scan:MaxHeight>{max_h}</scan:MaxHeight>
      <scan:SettingProfiles><scan:SettingProfile>
        <scan:ColorModes><scan:ColorMode>RGB24</scan:ColorMode><scan:ColorMode>Grayscale8</scan:ColorMode></scan:ColorModes>
        <scan:DocumentFormats><pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>
          <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat></scan:DocumentFormats>
        <scan:SupportedResolutions><scan:DiscreteResolutions>
          <scan:DiscreteResolution><scan:XResolution>200</scan:XResolution><scan:YResolution>200</scan:YResolution></scan:DiscreteResolution>
          <scan:DiscreteResolution><scan:XResolution>300</scan:XResolution><scan:YResolution>300</scan:YResolution></scan:DiscreteResolution>
          <scan:DiscreteResolution><scan:XResolution>600</scan:XResolution><scan:YResolution>600</scan:YResolution></scan:DiscreteResolution>
        </scan:DiscreteResolutions></scan:SupportedResolutions>
      </scan:SettingProfile></scan:SettingProfiles>
    </scan:{tag}>"""


CAPS_XML = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScannerCapabilities xmlns:scan="{NS}" xmlns:pwg="{PWG}">
  <pwg:Version>2.63</pwg:Version>
  <scan:Platen>{_input_caps("PlatenInputCaps", 2550, 3508)}</scan:Platen>
  <scan:Adf>{_input_caps("AdfSimplexInputCaps", 2550, 4200)}
    <scan:FeederCapacity>35</scan:FeederCapacity>
  </scan:Adf>
</scan:ScannerCapabilities>"""


def _pdf_page(label):
    img = Image.new("RGB", (300, 400), "white")
    ImageDraw.Draw(img).text((20, 20), label, fill="black")
    ImageDraw.Draw(img).rectangle((40, 80, 260, 360), fill="black")
    out = BytesIO()
    img.save(out, format="PDF")
    return out.getvalue()


class FakeDevice:
    def __init__(self):
        self.next_doc = [503, _pdf_page("1"), _pdf_page("2"), 404]
        self.post_codes = [201]
        self.requests = []  # (method, path)
        self.scan_xml = None
        self.deleted = []
        self.job_state = "Processing"
        self.status_state = "Idle"


@pytest.fixture
def device():
    dev = FakeDevice()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body=b"", ctype="text/xml", headers=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            dev.requests.append(("GET", self.path))
            if self.path == "/eSCL/ScannerCapabilities":
                return self._send(200, CAPS_XML.encode())
            if self.path == "/eSCL/ScannerStatus":
                body = f"""<scan:ScannerStatus xmlns:scan="{NS}" xmlns:pwg="{PWG}">
                  <pwg:State>{dev.status_state}</pwg:State><scan:AdfState>ScannerAdfLoaded</scan:AdfState>
                  <scan:Jobs><scan:JobInfo><pwg:JobUri>/eSCL/ScanJobs/job1</pwg:JobUri>
                  <pwg:JobState>{dev.job_state}</pwg:JobState></scan:JobInfo></scan:Jobs>
                </scan:ScannerStatus>"""
                return self._send(200, body.encode())
            if self.path == "/eSCL/ScanJobs/job1/NextDocument":
                item = dev.next_doc.pop(0) if dev.next_doc else 404
                if isinstance(item, int):
                    return self._send(item)
                return self._send(200, item, "application/pdf")
            self._send(404)

        def do_POST(self):
            dev.requests.append(("POST", self.path))
            n = int(self.headers.get("Content-Length", 0))
            dev.scan_xml = self.rfile.read(n)
            code = dev.post_codes.pop(0) if dev.post_codes else 201
            if code == 201:
                return self._send(201, headers={"Location": "/eSCL/ScanJobs/job1"})
            self._send(code)

        def do_DELETE(self):
            dev.requests.append(("DELETE", self.path))
            dev.deleted.append(self.path)
            self._send(200)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True)
    t.start()
    dev.info = ScannerInfo(name="fake", ip="127.0.0.1", port=srv.server_address[1], protocol="escl")
    yield dev
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def escl():
    s = EsclScanner()
    s.retry_delay = 0.01
    s.busy_wait = 0.2
    s.page_timeout = 2.0
    s.http_timeout = 5.0
    return s


def _req(xml):
    root = ET.fromstring(xml)
    return {el.tag.split("}")[1]: (el.text or "").strip() for el in root.iter()}


def test_capabilities_parsed(device, escl):
    caps = escl.get_capabilities(device.info)
    assert caps.sources == [ScanSource.FLATBED, ScanSource.ADF]
    assert caps.resolutions == [200, 300, 600]
    assert caps.formats == ["application/pdf", "image/jpeg"]
    assert caps.duplex is False
    assert caps.source_sizes == {"Platen": (2550, 3508), "Feeder": (2550, 4200)}


def test_adf_collects_all_pages_and_deletes_job(device, escl):
    pdf = escl.scan(device.info, ScanSettings(source=ScanSource.ADF))
    assert len(PdfReader(BytesIO(pdf)).pages) == 2
    assert device.deleted == ["/eSCL/ScanJobs/job1"]
    gets = [p for m, p in device.requests if p.endswith("NextDocument")]
    assert len(gets) == 4  # 503, 200, 200, 404


def test_flatbed_fetches_one_document(device, escl):
    pdf = escl.scan(device.info, ScanSettings(source=ScanSource.FLATBED))
    assert len(PdfReader(BytesIO(pdf)).pages) == 1
    assert device.deleted == ["/eSCL/ScanJobs/job1"]


def test_job_deleted_on_failure(device, escl):
    device.next_doc = [500]
    with pytest.raises(RuntimeError):
        escl.scan(device.info, ScanSettings(source=ScanSource.ADF))
    assert device.deleted == ["/eSCL/ScanJobs/job1"]


@pytest.mark.parametrize("page_size,source,expected", [
    ("a4", ScanSource.FLATBED, ("2480", "3508")),
    ("letter", ScanSource.FLATBED, ("2550", "3300")),
    ("legal", ScanSource.FLATBED, ("2550", "3508")),  # clamped to platen max
    ("legal", ScanSource.ADF, ("2550", "4200")),
    ("auto", ScanSource.FLATBED, ("2550", "3508")),
    ("auto", ScanSource.ADF, ("2550", "4200")),
])
def test_page_size_region(device, escl, page_size, source, expected):
    escl.scan(device.info, ScanSettings(source=source, page_size=page_size))
    req = _req(device.scan_xml)
    assert (req["Width"], req["Height"]) == expected
    assert req["ContentRegionUnits"] == "escl:ThreeHundredthsOfInches"


@pytest.mark.parametrize("mode,expected", [
    ("color", "RGB24"), ("gray", "Grayscale8"),
    ("bw", "Grayscale8"),  # device lacks BlackAndWhite1: falls back
])
def test_color_mapping(device, escl, mode, expected):
    escl.scan(device.info, ScanSettings(color_mode=mode))
    assert _req(device.scan_xml)["ColorMode"] == expected


def test_request_format_resolution_and_no_duplex(device, escl):
    escl.scan(device.info, ScanSettings(source=ScanSource.ADF, resolution=250, duplex=True))
    req = _req(device.scan_xml)
    assert req["XResolution"] == req["YResolution"] == "300"
    assert req["DocumentFormat"] == "application/pdf"
    assert req["InputSource"] == "Feeder"
    assert "Duplex" not in req


def test_post_409_waits_then_retries(device, escl):
    device.post_codes = [409, 201]
    pdf = escl.scan(device.info, ScanSettings(source=ScanSource.FLATBED))
    assert pdf.startswith(b"%PDF")
    assert [m for m, p in device.requests if m == "POST"] == ["POST", "POST"]
    assert ("GET", "/eSCL/ScannerStatus") in device.requests


def test_next_document_409_uses_job_state(device, escl):
    device.next_doc = [_pdf_page("1"), 409, 404]
    device.job_state = "Completed"
    pdf = escl.scan(device.info, ScanSettings(source=ScanSource.ADF))
    assert len(PdfReader(BytesIO(pdf)).pages) == 1
    device.next_doc = [409]
    device.job_state = "Aborted"
    with pytest.raises(RuntimeError, match="aborted"):
        escl.scan(device.info, ScanSettings(source=ScanSource.ADF))


def test_jpeg_only_device_and_jpeg_pages_combined(device, escl):
    global CAPS_XML
    jpg = BytesIO()
    Image.new("RGB", (200, 300), "black").save(jpg, format="JPEG")
    device.next_doc = [jpg.getvalue(), jpg.getvalue(), 404]
    old = CAPS_XML
    try:
        CAPS_XML = old.replace("<pwg:DocumentFormat>application/pdf</pwg:DocumentFormat>", "")
        pdf = escl.scan(device.info, ScanSettings(source=ScanSource.ADF))
    finally:
        CAPS_XML = old
    assert _req(device.scan_xml)["DocumentFormat"] == "image/jpeg"
    assert len(PdfReader(BytesIO(pdf)).pages) == 2


def test_status_and_odd_xml(device, escl):
    st = escl.get_status(device.info)
    assert st.state == ScannerState.IDLE and st.adf_state.value == "ScannerAdfLoaded"
    weird = ET.fromstring(f'<scan:ScannerCapabilities xmlns:scan="{NS}"><scan:Platen>'
                          '<scan:PlatenInputCaps><scan:MaxWidth>abc</scan:MaxWidth>'
                          '<scan:XResolution>x</scan:XResolution></scan:PlatenInputCaps>'
                          '</scan:Platen></scan:ScannerCapabilities>')
    caps = parse_capabilities(weird)
    assert caps.sources == [ScanSource.FLATBED] and caps.resolutions == [300]


def test_offline_status():
    st = EsclScanner().get_status(ScannerInfo(name="x", ip="127.0.0.1", port=1, protocol="escl"))
    assert st.state == ScannerState.OFFLINE


def test_ipv6_base_url():
    assert ScannerInfo(name="x", ip="fe80::1", port=80, protocol="escl").base_url == "http://[fe80::1]:80"
