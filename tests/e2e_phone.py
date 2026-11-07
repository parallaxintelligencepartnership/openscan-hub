"""Browser check of the phone web app. Not part of the pytest run.

It starts OpenScanHub against a fake eSCL scanner, opens /m in a phone-sized
Chromium and walks the whole flow. It needs the `playwright` package and a
Chromium build. Run it from the repository root:

    CHROMIUM=/path/to/chrome python tests/e2e_phone.py

Optional: `opencv-python-headless` (reads the QR code the dashboard shows).
"""

import os
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from PIL import Image  # noqa: E402
from pypdf import PdfReader  # noqa: E402

from test_escl import CAPS_XML, NS, PWG  # noqa: E402

ADMIN_TOKEN = "0123456789abcdef0123456789abcdef"
SCAN_TOKEN = "fedcba9876543210fedcba9876543210"
SIZES = [(200, 300), (210, 300), (220, 300)]  # the three scanned pages, by point size


def pdf_page(size):
    out = BytesIO()
    Image.new("RGB", size, "white").save(out, format="PDF")
    return out.getvalue()


class Device:
    jobs = 0
    served = False


def start_fake_scanner():
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
            if self.path == "/eSCL/ScannerCapabilities":
                return self._send(200, CAPS_XML.encode())
            if self.path == "/eSCL/ScannerStatus":
                body = (f'<scan:ScannerStatus xmlns:scan="{NS}" xmlns:pwg="{PWG}"><pwg:State>Idle</pwg:State>'
                        f'<scan:AdfState>ScannerAdfEmpty</scan:AdfState></scan:ScannerStatus>')
                return self._send(200, body.encode())
            if self.path == "/eSCL/ScanJobs/job1/NextDocument":
                if Device.served:
                    return self._send(404)
                Device.served = True
                return self._send(200, pdf_page(SIZES[(Device.jobs - 1) % 3]), "application/pdf")
            self._send(404)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            Device.jobs += 1
            Device.served = False
            self._send(201, headers={"Location": "/eSCL/ScanJobs/job1"})

        def do_DELETE(self):
            self._send(200)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=srv.serve_forever, args=(0.05,), daemon=True).start()
    return srv.server_address[1]


def start_app(work: Path, scanner_port: int):
    os.environ["OPENSCAN_CONFIG_DIR"] = str(work / "config")
    os.environ["OPENSCAN_DISABLE_LOCAL_AUTOLOGIN"] = "1"
    (work / "config").mkdir()
    from openscan import profiles
    from openscan.config import AppConfig
    from openscan.web import server as srv

    cfg = AppConfig()
    cfg.wizard_completed = True
    cfg.web.bind, cfg.web.port = "127.0.0.1", 0
    cfg.web.api_token, cfg.web.scan_token = ADMIN_TOKEN, SCAN_TOKEN
    cfg.web.advertise_mdns = False
    cfg.output.folder = str(work / "default")
    cfg.scanner.ip, cfg.scanner.port, cfg.scanner.protocol = "127.0.0.1", scanner_port, "escl"
    cfg.scanner.name = "Fake scanner"
    letters = profiles.Profile(
        id="letters", name="Letters", source="flatbed",
        destinations=[{"type": "folder", "options": {"folder": str(work / "letters")}}],
        name_rule="phone_{n}")
    cfg.profiles = [letters]
    cfg.default_profile_id = "letters"
    server = srv.create_server(cfg)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return server.server_address[1]


def effective_sizes(path):
    out = []
    for p in PdfReader(str(path)).pages:
        w, h = round(float(p.mediabox.width)), round(float(p.mediabox.height))
        out.append((h, w) if p.rotation % 180 else (w, h))
    return out


def check(cond, message):
    print(("ok   " if cond else "FAIL ") + message)
    if not cond:
        check.failed.append(message)


check.failed = []


def main():
    from playwright.sync_api import sync_playwright

    work = Path(tempfile.mkdtemp(prefix="phone-e2e-"))
    port = start_app(work, start_fake_scanner())
    base = f"http://localhost:{port}"
    photo = work / "photo.jpg"
    Image.new("RGB", (1100, 800), "gray").save(photo, format="JPEG")  # 100 dpi -> 792 x 576 pt

    with sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=os.environ.get("CHROMIUM") or None,
                                     args=["--no-sandbox"])
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True,
                                  device_scale_factor=2, color_scheme="light")
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on("console", lambda m: errors.append(m.text) if m.type == "error" and "401" not in m.text and "INTERNET_DISCONNECTED" not in m.text else None)

        # Sign in with the scan token.
        page.goto(f"{base}/m")
        page.wait_for_selector("#view-login:not(.hidden)")
        page.fill("#token", "wrong-token")
        page.tap("#login-form button")
        page.wait_for_selector("#notice:not(.hidden)")
        page.fill("#token", SCAN_TOKEN)
        page.tap("#login-form button")
        page.wait_for_selector("#view-home:not(.hidden)")
        check(page.inner_text("#role-note") == "Scan only", "signed in with the scan token")
        page.wait_for_function("document.getElementById('status-name').textContent === 'Fake scanner'")
        check("Ready" in page.inner_text("#status-detail"), "scanner status shows Ready")
        check(page.locator(".profile-btn").count() == 1, "profile button is shown")
        check(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"),
              "no sideways scroll at 390 px")

        # Scan three pages.
        page.tap("#btn-scan-pages")
        page.wait_for_selector("#view-session:not(.hidden)")
        for n in (1, 2, 3):
            page.tap("#btn-scan-next")
            page.wait_for_function(f"document.querySelectorAll('#page-list .page:not(.placeholder)').length === {n}")
            page.wait_for_function("Array.from(document.querySelectorAll('#page-list img')).every(i => i.complete && i.naturalWidth > 0)")
        check(True, "three pages scanned, thumbnails loaded")

        # Move page 3 to the front by dragging its handle.
        IDS = "Array.from(document.querySelectorAll('#page-list img')).map(i => i.src.split('v=')[1])"
        before = page.evaluate(IDS)
        handle = page.locator("#page-list .page").nth(2).locator(".drag").bounding_box()
        top = page.locator("#page-list .page").nth(0).bounding_box()
        page.mouse.move(handle["x"] + 20, handle["y"] + 20)
        page.mouse.down()
        page.mouse.move(handle["x"] + 20, top["y"] + 10, steps=12)
        page.mouse.up()
        page.wait_for_timeout(1000)
        ids = page.evaluate(IDS)
        check(ids == [before[2], before[0], before[1]], f"drag moved page 3 to the front {before} -> {ids}")
        # Up and down buttons: move the first page down, then back up.
        page.tap("#page-list .page:nth-child(1) .down")
        page.wait_for_timeout(300)
        page.tap("#page-list .page:nth-child(2) .up")
        page.wait_for_timeout(300)
        # Order now C, A, B. Rotate A, delete B.
        page.tap("#page-list .page:nth-child(2) .rotate")
        page.wait_for_timeout(500)
        page.tap("#page-list .page:nth-child(3) .del")
        page.wait_for_function("document.querySelectorAll('#page-list .page').length === 2")

        # Add a photo with the camera input.
        page.set_input_files("#camera-session", str(photo))
        page.wait_for_function("document.querySelectorAll('#page-list .page').length === 3")
        page.wait_for_function("Array.from(document.querySelectorAll('#page-list img')).every(i => i.complete && i.naturalWidth > 0)")
        check(page.locator("#camera-session").get_attribute("capture") == "environment"
              and page.locator("#camera-session").get_attribute("multiple") is not None
              and page.locator("#camera-session").get_attribute("accept") == "image/*,application/pdf",
              "camera input has the required attributes")

        page.tap("#btn-save")
        page.wait_for_selector("#view-home:not(.hidden)")
        page.wait_for_selector("#notice.ok")
        out = sorted((work / "letters").glob("*.pdf"))
        check(len(out) == 1, "one PDF delivered to the profile folder")
        sizes = effective_sizes(out[0])
        check(len(sizes) == 3, f"delivered PDF has 3 pages ({len(sizes)})")
        expect = [SIZES[2], (SIZES[0][1], SIZES[0][0]), (792, 576)]
        check(sizes == expect, f"page order and rotation are right: {sizes} == {expect}")

        # A one-tap profile scan.
        page.tap(".profile-btn")
        page.wait_for_selector("#notice.ok")
        check(len(list((work / "letters").glob("*.pdf"))) == 2, "one-tap profile scan delivered a file")

        # Camera on the home screen starts a session by itself.
        page.set_input_files("#camera-home", str(photo))
        page.wait_for_function("document.querySelectorAll('#page-list .page').length === 1")
        page.tap("#btn-cancel")
        page.once("dialog", lambda d: d.accept())

        # Manifest, service worker, installability.
        page.goto(f"{base}/m")
        page.wait_for_selector("#view-home:not(.hidden), #view-session:not(.hidden)")
        page.evaluate("navigator.serviceWorker.ready.then(() => 1)")
        check(page.evaluate("navigator.serviceWorker.getRegistration().then(r => !!(r && r.active))"),
              "service worker is active")
        keys = page.evaluate("caches.keys().then(async ks => (await Promise.all(ks.map(async k => (await (await caches.open(k)).keys()).map(r => new URL(r.url).pathname)))).flat())")
        check(keys and not any("/api/" in k for k in keys), f"cache holds only shell files: {sorted(keys)}")
        # Chromium reports installability only outside a private window, so use a profile folder.
        real = pw.chromium.launch_persistent_context(
            str(work / "profile"), executable_path=os.environ.get("CHROMIUM") or None,
            args=["--no-sandbox"], viewport={"width": 390, "height": 844})
        rp = real.pages[0] if real.pages else real.new_page()
        rp.goto(f"{base}/m")
        rp.wait_for_selector("#view-login:not(.hidden)")
        rp.evaluate("navigator.serviceWorker.ready.then(() => 1)")
        cdp = real.new_cdp_session(rp)
        cdp.send("Page.enable")
        manifest = cdp.send("Page.getAppManifest")
        check(not manifest.get("errors"), f"manifest has no errors {manifest.get('errors')}")
        problems = cdp.send("Page.getInstallabilityErrors")["installabilityErrors"]
        check(not problems, f"installability errors: {problems}")
        real.close()
        ctx.set_offline(True)
        page.reload()
        check("OpenScanHub" in page.title() and page.locator("#view-login").count() == 1,
              "the shell loads offline from the cache")
        ctx.set_offline(False)

        # Small phone, dark theme.
        small = browser.new_context(viewport={"width": 360, "height": 740}, has_touch=True, is_mobile=True,
                                    color_scheme="dark")
        sp = small.new_page()
        sp.goto(f"{base}/m#token={SCAN_TOKEN}")
        sp.wait_for_selector("#view-home:not(.hidden)")
        check(sp.evaluate("location.hash") == "", "token in the address fragment signs in and is removed")
        check(sp.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"),
              "no sideways scroll at 360 px")
        dark = sp.evaluate("getComputedStyle(document.body).backgroundColor")
        light = page.evaluate("getComputedStyle(document.body).backgroundColor")
        check(dark != light, f"dark theme differs from light ({dark} vs {light})")
        small_buttons = sp.evaluate("Array.from(document.querySelectorAll('.btn')).filter(b => b.offsetParent).every(b => b.getBoundingClientRect().height >= 44)")
        check(small_buttons, "buttons are at least 44 px tall")
        small.close()

        # The scan token cannot reach admin pages or routes.
        r = ctx.request.get(f"{base}/api/config")
        check(r.status == 403, f"scan token gets 403 on /api/config ({r.status})")

        # Dashboard QR code, as admin.
        admin = browser.new_context(viewport={"width": 1000, "height": 900})
        ap = admin.new_page()
        ap.goto(f"{base}/login")
        ap.fill("#token", ADMIN_TOKEN)
        ap.click("#login-form button")
        ap.wait_for_url("**/dashboard")
        ap.wait_for_selector("#phone-card:not(.hidden)")
        plain = ap.locator("#phone-qr").screenshot()
        ap.click("#phone-code-btn")
        ap.wait_for_selector("#phone-code-warning:not(.hidden)")
        withtoken = ap.locator("#phone-qr").screenshot()
        check(plain != withtoken, "Show phone code changes the code")
        check(ADMIN_TOKEN not in ap.content(), "the admin token is not in the dashboard page")
        try:
            import cv2
            import numpy as np
            det = cv2.QRCodeDetector()
            texts = []
            for png in (plain, withtoken):
                img = cv2.imdecode(np.frombuffer(png, np.uint8), cv2.IMREAD_COLOR)
                texts.append(det.detectAndDecode(img)[0])
            check(texts[0] == f"{base}/m", f"plain code decodes to the page address: {texts[0]}")
            check(texts[1] == f"{base}/m#token={SCAN_TOKEN}", "token code carries only the scan token")
            check(ADMIN_TOKEN not in texts[1], "admin token is not in the code")
        except ImportError:
            print("skip QR decode (no opencv)")
        browser.close()

        check(not errors, f"no page errors: {errors}")

    print("FAILED: %d" % len(check.failed) if check.failed else "ALL OK")
    return 1 if check.failed else 0


if __name__ == "__main__":
    sys.exit(main())
