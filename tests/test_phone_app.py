"""The phone web app: the /m page, its files, and the multipage routes it no longer uses."""

from io import BytesIO

import pytest
from conftest import LAN_PEER, TOKEN
from PIL import Image
from pypdf import PdfReader

from openscan import multipage
from openscan.web import server as srv

SCAN_TOKEN = "fedcba9876543210fedcba9876543210"


@pytest.fixture(autouse=True)
def _reset():
    multipage._sessions.clear()
    yield
    multipage._sessions.clear()


def _pdf(n=1, size=(120, 160)):
    out = BytesIO()
    frames = [Image.new("RGB", size, "white") for _ in range(n)]
    frames[0].save(out, format="PDF", save_all=True, append_images=frames[1:])
    return out.getvalue()


# --- routes ----------------------------------------------------------------

@pytest.fixture
def scan_server(live_server, app_config, fake_scanner):
    app_config.web.scan_token = SCAN_TOKEN
    fake_scanner.online = True
    live_server.set_peer(LAN_PEER)
    return live_server


def _start(srv_, token=SCAN_TOKEN, **body):
    r = srv_.post("/api/multipage/start", body, token=token)
    assert r.status == 200, r.body
    return r.json()["session_id"]


def test_phone_page_and_files(scan_server):
    s = scan_server
    r = s.get("/m")  # no sign-in: the page asks for the token itself
    assert r.status == 200 and b"manifest.webmanifest" in r.body
    assert s.get("/m", token=SCAN_TOKEN).status == 200
    assert s.get("/m", cookie=s.post("/api/login", {"token": SCAN_TOKEN}).cookie()).status == 200
    man = s.get("/manifest.webmanifest")
    assert man.headers["Content-Type"].startswith("application/manifest+json")
    data = man.json()
    assert data["display"] == "standalone" and data["start_url"] == "m"
    sizes = {i["sizes"] for i in data["icons"]}
    assert {"192x192", "512x512"} <= sizes
    for icon in data["icons"]:
        png = s.get("/" + icon["src"])
        assert png.status == 200
        assert Image.open(BytesIO(png.body)).size == tuple(map(int, icon["sizes"].split("x")))
    sw = s.get("/sw.js")
    assert sw.status == 200 and "javascript" in sw.headers["Content-Type"]
    # The worker caches only the static shell: its list holds no API path.
    shell = sw.body.decode().split("const SHELL")[1].split("];")[0]
    assert "api" not in shell and "'m'" in shell
    for path in ("/css/m.css", "/js/m.js", "/js/qr.js"):
        assert s.get(path).status == 200


def test_phone_page_is_in_the_scan_role_page_list():
    assert "/m" in srv.SCAN_ROLE_PAGES


def test_start_with_profile_and_save_through_it(scan_server, tmp_path):
    s = scan_server
    created = s.post("/api/profiles", {
        "name": "Invoices", "source": "flatbed", "show_on_home": True,
        "destinations": [{"type": "folder", "options": {"folder": str(tmp_path / "inv")}}],
        "name_rule": "inv_{n}"}, token=TOKEN)
    assert created.status == 201, created.body
    pid = created.json()["profile"]["id"]
    assert s.post("/api/multipage/start", {"profile_id": "nope"}, token=SCAN_TOKEN).status == 404
    r = s.post("/api/multipage/start", {"profile_id": pid}, token=SCAN_TOKEN)
    sid = r.json()["session_id"]
    assert r.json()["profile_id"] == pid
    assert s.get(f"/api/multipage/status/{sid}", token=SCAN_TOKEN).json()["profile_id"] == pid
    multipage.add_page(sid, _pdf())
    multipage.add_page(sid, _pdf())
    saved = s.post("/api/multipage/save", {"session_id": sid}, token=SCAN_TOKEN)
    assert saved.status == 200 and saved.json()["success"], saved.body
    out = tmp_path / "inv" / "inv_001.pdf"
    assert len(PdfReader(str(out)).pages) == 2
    assert s.get(f"/api/multipage/status/{sid}", token=SCAN_TOKEN).status == 404


def test_save_can_name_another_profile(scan_server, tmp_path):
    s = scan_server
    pid = s.post("/api/profiles", {
        "name": "Other", "destinations": [{"type": "folder", "options": {"folder": str(tmp_path / "o")}}],
        "name_rule": "o_{n}"}, token=TOKEN).json()["profile"]["id"]
    sid = _start(s)  # no profile at start
    multipage.add_page(sid, _pdf())
    assert s.post("/api/multipage/save", {"session_id": sid, "profile_id": "nope"},
                  token=SCAN_TOKEN).status == 404
    assert s.post("/api/multipage/save", {"session_id": sid, "profile_id": pid},
                  token=SCAN_TOKEN).json()["success"]
    assert (tmp_path / "o" / "o_001.pdf").exists()


def test_scan_page_uses_the_session_profile(scan_server, fake_scanner, tmp_path):
    s = scan_server
    pid = s.post("/api/profiles", {"name": "Gray", "source": "flatbed", "color_mode": "gray",
                                   "resolution": 150}, token=TOKEN).json()["profile"]["id"]
    sid = s.post("/api/multipage/start", {"profile_id": pid}, token=SCAN_TOKEN).json()["session_id"]
    fake_scanner.data = _pdf()
    assert s.post("/api/multipage/scan", {"session_id": sid}, token=SCAN_TOKEN).status == 200
    assert (fake_scanner.scans[-1].color_mode, fake_scanner.scans[-1].resolution) == ("gray", 150)


def test_old_start_and_save_still_work(scan_server, fake_scanner):
    sid = _start(scan_server, source="flatbed")
    assert scan_server.post("/api/multipage/scan", {"session_id": sid}, token=SCAN_TOKEN).json()["page_count"] == 1
    assert scan_server.post("/api/multipage/save", {"session_id": sid}, token=SCAN_TOKEN).status == 200


def test_static_files_ship():
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    assert '"openscan.web.static" = ["**/*"]' in (root / "pyproject.toml").read_text()
    static = root / "openscan" / "web" / "static"
    for rel in ("templates/m.html", "manifest.webmanifest", "sw.js", "js/m.js", "js/qr.js",
                "css/m.css", "icons/icon-192.png", "icons/icon-512.png"):
        assert (static / rel).is_file(), rel


@pytest.mark.parametrize("path", [
    "/api/multipage/reorder", "/api/multipage/rotate",
    "/api/multipage/delete-page", "/api/multipage/upload"])
def test_phone_editing_routes_are_gone(scan_server, path):
    sid = _start(scan_server)
    r = scan_server.post(path, {"session_id": sid}, token=TOKEN)
    assert r.status == 404


def test_upload_only_start_is_gone(live_server, app_config):
    app_config.scanner.ip = ""
    assert live_server.post("/api/multipage/start", {"upload_only": True}, token=TOKEN).status == 400
