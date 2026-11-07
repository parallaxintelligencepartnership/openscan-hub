"""Browser check of the phone web app. Not part of the pytest run.

It starts OpenScanHub against a fake eSCL scanner with the feeder monitor on,
opens /m in a phone-sized Chromium and walks the whole flow: pick a profile,
put "paper" in the fake feeder, and check which profile the file used. It needs the `playwright` package and a
Chromium build. Run it from the repository root:

    CHROMIUM=/path/to/chrome python tests/e2e_phone.py

Optional: `opencv-python-headless` (reads the QR code the dashboard shows).
"""

import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from PIL import Image  # noqa: E402

from test_escl import CAPS_XML, NS, PWG  # noqa: E402

ADMIN_TOKEN = "0123456789abcdef0123456789abcdef"
SCAN_TOKEN = "fedcba9876543210fedcba9876543210"


def pdf_page(size):
    out = BytesIO()
    Image.new("RGB", size, "white").save(out, format="PDF")
    return out.getvalue()


class Device:
    jobs = 0
    served = False
    adf = "ScannerAdfEmpty"  # the fake feeder; a scan job empties it


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
                        f'<scan:AdfState>{Device.adf}</scan:AdfState></scan:ScannerStatus>')
                return self._send(200, body.encode())
            if self.path == "/eSCL/ScanJobs/job1/NextDocument":
                if Device.served:
                    return self._send(404)
                Device.served = True
                return self._send(200, pdf_page((200, 300)), "application/pdf")
            self._send(404)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            Device.jobs += 1
            Device.served = False
            Device.adf = "ScannerAdfEmpty"
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
    default = profiles.Profile(
        id="default", name="Default", source="adf", icon="D",
        destinations=[{"type": "folder", "options": {"folder": str(work / "default")}}],
        name_rule="default_{n}")
    letters = profiles.Profile(
        id="letters", name="Letters", source="adf", color_mode="gray",
        destinations=[{"type": "folder", "options": {"folder": str(work / "letters")}}],
        name_rule="letters_{n}")
    cfg.profiles = [default, letters]
    cfg.default_profile_id = "default"
    cfg.monitor.enabled = True
    cfg.monitor.poll_interval = 0.3
    cfg.monitor.settle_time = 0.2
    server = srv.create_server(cfg)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    from openscan import monitor
    monitor.start_monitor(cfg)
    return server.server_address[1]


def wait_files(folder, count, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        if len(list(folder.glob("*.pdf"))) >= count:
            return
        time.sleep(0.2)


def api_next(page, base):
    """The next-feed profile the server holds now."""
    return page.evaluate("fetch('api/feed').then(r => r.json()).then(d => d.next_profile)")


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
        page.wait_for_function("document.getElementById('status-detail').textContent.includes('Feeder is empty')")
        check("Ready" in page.inner_text("#status-detail"), "scanner and feeder status show at the top")
        check(page.locator(".profile-btn").count() == 2, "both profile buttons are shown")
        check(page.locator(".profile-btn").first.bounding_box()["height"] >= 44, "profile buttons are 44 px or taller")
        check(page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth"),
              "no sideways scroll at 390 px")
        check(page.locator("#view-session").count() == 0 and page.locator("#btn-scan-pages").count() == 0,
              "the phone page has no manual scan or page editing")

        # Tap a profile: the next feed shows, a second tap clears it, Cancel clears it.
        letters = page.locator('.profile-btn[data-id="letters"]')
        letters.tap()
        page.wait_for_selector("#next-feed:not(.hidden)")
        check(page.inner_text("#next-text") == "Next feed: Letters. Load the paper.", "next feed message shows")
        check("Ends in" in page.inner_text("#next-count"), "countdown shows")
        check(letters.get_attribute("aria-pressed") == "true", "active profile button is marked")
        letters.tap()
        page.wait_for_selector("#next-feed.hidden", state="attached")
        check(api_next(page, base) is None, "tapping the active profile clears the next feed")
        letters.tap()
        page.wait_for_selector("#next-feed:not(.hidden)")
        page.tap("#btn-cancel-feed")
        page.wait_for_selector("#next-feed.hidden", state="attached")
        check(api_next(page, base) is None, "Cancel clears the next feed")

        # Put paper in the fake feeder. The monitor scans it with Letters.
        letters.tap()
        page.wait_for_selector("#next-feed:not(.hidden)")
        page.wait_for_timeout(1000)  # let the monitor see the empty feeder first
        Device.adf = "ScannerAdfLoaded"
        wait_files(work / "letters", 1)
        check(len(list((work / "letters").glob("*.pdf"))) == 1, "the batch used the Letters profile folder")
        check(not list((work / "default").glob("*.pdf")), "nothing went to the Default folder")
        page.wait_for_selector("#next-feed.hidden", state="attached", timeout=10000)
        check(True, "the next feed cleared after the batch")
        page.wait_for_function("document.querySelectorAll('#last-scans li').length === 1", timeout=10000)
        check("Letters" in page.inner_text("#last-scans"), "last scans list shows the profile")

        # Next batch, nothing picked: Default.
        page.wait_for_timeout(1500)
        Device.adf = "ScannerAdfLoaded"
        wait_files(work / "default", 1)
        check(len(list((work / "default").glob("*.pdf"))) == 1, "the next batch used Default")
        page.wait_for_function("document.querySelectorAll('#last-scans li').length === 2", timeout=10000)
        check(len(list((work / "letters").glob("*.pdf"))) == 1, "Letters did not get a second file")

        # Manifest, service worker, installability.
        page.goto(f"{base}/m")
        page.wait_for_selector("#view-home:not(.hidden)")
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
        ap.wait_for_selector("#profile-buttons:not(.hidden)")
        ap.click('.profile-button[data-id="letters"]')
        ap.wait_for_function("document.querySelector('.profile-button[data-id=letters]').getAttribute('aria-pressed') === 'true'")
        nxt = ap.evaluate("fetch('api/feed').then(r => r.json()).then(d => d.next_profile && d.next_profile.id)")
        check(nxt == "letters", "a dashboard profile button sets the next feed")
        check(len(list((work / "letters").glob("*.pdf"))) == 1, "the dashboard button did not start a scan")
        ap.click('.profile-button[data-id="letters"]')
        ap.wait_for_function("document.querySelector('.profile-button[data-id=letters]').getAttribute('aria-pressed') === 'false'")
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
