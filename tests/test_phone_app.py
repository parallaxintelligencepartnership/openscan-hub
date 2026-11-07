"""The phone web app: the /m page, its files, page editing and camera upload."""

import json
from io import BytesIO

import pytest
from conftest import LAN_PEER, TOKEN
from PIL import Image
from pypdf import PdfReader

from openscan import multipage
from openscan.scanner.base import ScanSource
from openscan.web import server as srv

SCAN_TOKEN = "fedcba9876543210fedcba9876543210"


@pytest.fixture(autouse=True)
def _reset():
    multipage._sessions.clear()
    yield
    multipage._sessions.clear()


def _image(color="white", size=(120, 160), fmt="JPEG"):
    out = BytesIO()
    Image.new("RGB", size, color).save(out, format=fmt)
    return out.getvalue()


def _pdf(n=1, size=(120, 160)):
    out = BytesIO()
    frames = [Image.new("RGB", size, "white") for _ in range(n)]
    frames[0].save(out, format="PDF", save_all=True, append_images=frames[1:])
    return out.getvalue()


def _sizes(pdf: bytes):
    """Page sizes as a viewer shows them, with the page turn applied."""
    out = []
    for p in PdfReader(BytesIO(pdf)).pages:
        w, h = round(float(p.mediabox.width)), round(float(p.mediabox.height))
        out.append((h, w) if p.rotation % 180 else (w, h))
    return out


def _new_session(n=0):
    sid = multipage.create_session(ScanSource.FLATBED)
    for i in range(n):
        multipage.add_page(sid, _pdf(size=(100 + 10 * i, 200)))
    return sid


# --- backend ---------------------------------------------------------------

def test_reorder_rotate_delete():
    sid = _new_session(3)
    assert multipage.reorder_pages(sid, [2, 0, 1]) == 3
    assert _sizes(multipage.merge_pages(sid)) == [(120, 200), (100, 200), (110, 200)]
    multipage.rotate_page(sid, 0, 90)
    assert _sizes(multipage.merge_pages(sid))[0] == (200, 120)
    multipage.rotate_page(sid, 0, -90)
    assert _sizes(multipage.merge_pages(sid))[0] == (120, 200)
    assert multipage.delete_page(sid, 1) == 2
    assert multipage.get_session_info(sid)["page_count"] == 2


@pytest.mark.parametrize("order", [[0, 1], [0, 0, 1], [0, 1, 5], "012", [0, 1, True], []])
def test_reorder_rejects_bad_order(order):
    sid = _new_session(3)
    with pytest.raises(IndexError):
        multipage.reorder_pages(sid, order)


@pytest.mark.parametrize("degrees", [0, 45, 360, "90", None, True])
def test_rotate_rejects_bad_degrees(degrees):
    sid = _new_session(1)
    with pytest.raises(ValueError):
        multipage.rotate_page(sid, 0, degrees)


def test_page_edits_reject_bad_index_and_session():
    sid = _new_session(2)
    for bad in (-1, 2, "0", None):
        with pytest.raises(IndexError):
            multipage.delete_page(sid, bad)
        with pytest.raises(IndexError):
            multipage.rotate_page(sid, bad, 90)
    with pytest.raises(ValueError):
        multipage.delete_page("nope", 0)


def test_page_ids_change_when_a_page_changes():
    sid = _new_session(2)
    before = multipage.get_session_info(sid)["pages"]
    multipage.rotate_page(sid, 1, 90)
    after = multipage.get_session_info(sid)["pages"]
    assert before[0] == after[0] and before[1] != after[1]


def test_upload_images_and_pdf():
    sid = _new_session()
    assert multipage.add_upload(sid, _image(), "image/jpeg") == 1
    assert multipage.add_upload(sid, _image(fmt="PNG"), "image/png") == 2
    assert multipage.add_upload(sid, _pdf(3), "application/pdf") == 5
    assert len(PdfReader(BytesIO(multipage.merge_pages(sid))).pages) == 5


def test_upload_applies_the_exif_turn():
    img = Image.new("RGB", (160, 120), "white")
    exif = img.getexif()
    exif[0x0112] = 6  # the camera was held sideways
    out = BytesIO()
    img.save(out, format="JPEG", exif=exif)
    sid = _new_session()
    multipage.add_upload(sid, out.getvalue(), "image/jpeg")
    [(w, h)] = _sizes(multipage.merge_pages(sid))
    assert h > w


@pytest.mark.parametrize("data,ctype", [
    (b"", "image/jpeg"),
    (b"not an image at all", "image/jpeg"),
    (b"GIF89a" + b"\0" * 20, "image/gif"),
    (_image(), "text/html"),
    (b"%PDF-1.4 broken", "application/pdf"),
])
def test_upload_refuses_bad_files(data, ctype):
    sid = _new_session()
    with pytest.raises(multipage.UploadError):
        multipage.add_upload(sid, data, ctype)
    assert multipage.get_session_info(sid)["page_count"] == 0


def test_upload_refuses_gif_and_big_files(monkeypatch):
    sid = _new_session()
    out = BytesIO()
    Image.new("RGB", (10, 10)).save(out, format="GIF")
    with pytest.raises(multipage.UploadError):
        multipage.add_upload(sid, out.getvalue(), "image/jpeg")
    monkeypatch.setattr(multipage, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(multipage.UploadError, match="25 MB"):
        multipage.add_upload(sid, _image(size=(400, 400)), "image/jpeg")


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


def _upload(srv_, sid, data, ctype="image/jpeg", token=SCAN_TOKEN):
    return srv_.request("POST", f"/api/multipage/upload?session_id={sid}", body=data,
                        headers={"Content-Type": ctype}, token=token)


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


def test_scan_token_can_use_the_new_routes():
    for sub in ("reorder", "rotate", "delete-page", "upload"):
        assert srv.scan_role_allows("POST", f"/api/multipage/{sub}")


def test_edit_routes(scan_server):
    s = scan_server
    sid = _start(s)
    for i in range(3):
        multipage.add_page(sid, _pdf(size=(100 + 10 * i, 200)))
    r = s.post("/api/multipage/reorder", {"session_id": sid, "order": [2, 0, 1]}, token=SCAN_TOKEN)
    assert r.status == 200 and r.json()["page_count"] == 3 and len(r.json()["pages"]) == 3
    assert _sizes(multipage.merge_pages(sid))[0] == (120, 200)
    assert s.post("/api/multipage/reorder", {"session_id": sid, "order": [0]}, token=SCAN_TOKEN).status == 400
    assert s.post("/api/multipage/rotate", {"session_id": sid, "page_index": 0, "degrees": 90},
                  token=SCAN_TOKEN).status == 200
    assert _sizes(multipage.merge_pages(sid))[0] == (200, 120)
    assert s.post("/api/multipage/rotate", {"session_id": sid, "page_index": 0, "degrees": 33},
                  token=SCAN_TOKEN).status == 400
    r = s.post("/api/multipage/delete-page", {"session_id": sid, "page_index": 1}, token=SCAN_TOKEN)
    assert r.status == 200 and r.json()["page_count"] == 2
    assert s.post("/api/multipage/delete-page", {"session_id": sid, "page_index": 9},
                  token=SCAN_TOKEN).status == 400
    assert s.post("/api/multipage/delete-page", {"session_id": "nope", "page_index": 0},
                  token=SCAN_TOKEN).status == 404
    assert s.post("/api/multipage/delete-page", {"page_index": 0}, token=SCAN_TOKEN).status == 400


def test_upload_route(scan_server):
    s = scan_server
    sid = _start(s)
    r = _upload(s, sid, _image())
    assert r.status == 200 and r.json()["page_count"] == 1
    assert _upload(s, sid, _pdf(2), "application/pdf").json()["page_count"] == 3
    assert _upload(s, sid, b"junk").status == 415
    assert _upload(s, sid, _image(), "text/plain").status == 415
    assert _upload(s, "nope", _image()).status == 404
    assert _upload(s, sid, b"").status == 400
    assert s.request("POST", "/api/multipage/upload", body=_image(),
                     headers={"Content-Type": "image/jpeg"}, token=SCAN_TOKEN).status == 400
    assert _upload(s, sid, _image(), token=None).status == 401


def test_upload_over_the_limit_is_refused_before_reading(scan_server, monkeypatch):
    sid = _start(scan_server)
    monkeypatch.setattr(multipage, "MAX_UPLOAD_BYTES", 50)
    r = _upload(scan_server, sid, b"x" * 51)
    assert r.status == 413 and "25 MB" in r.json()["error"]


def test_upload_needs_the_same_origin(scan_server):
    sid = _start(scan_server)
    r = scan_server.request("POST", f"/api/multipage/upload?session_id={sid}", body=_image(),
                            headers={"Content-Type": "image/jpeg", "Origin": "http://evil.example"},
                            token=SCAN_TOKEN)
    assert r.status == 403


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
    _upload(s, sid, _image())
    _upload(s, sid, _image("black"))
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
    _upload(s, sid, _image())
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


def test_upload_only_session_needs_no_scanner(live_server, app_config):
    app_config.scanner.ip = ""
    r = live_server.post("/api/multipage/start", {}, token=TOKEN)
    assert r.status == 400
    r = live_server.post("/api/multipage/start", {"upload_only": True}, token=TOKEN)
    assert r.status == 200
    sid = r.json()["session_id"]
    assert _upload(live_server, sid, _image(), token=TOKEN).status == 200


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
