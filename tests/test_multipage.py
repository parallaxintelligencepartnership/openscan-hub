import threading
from datetime import datetime, timedelta
from io import BytesIO

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader

from openscan import multipage
from openscan.scanner.base import ScanSource, release_scan, scan_lock


@pytest.fixture(autouse=True)
def _reset():
    multipage._sessions.clear()
    yield
    multipage._sessions.clear()


def _jpeg(blank=False):
    img = Image.new("RGB", (600, 800), "white")
    if not blank:
        ImageDraw.Draw(img).rectangle((100, 100, 500, 700), fill="black")
    out = BytesIO()
    img.save(out, format="JPEG")
    return out.getvalue()


def test_scan_page_adds_and_merges_with_blank_removal():
    sid = multipage.create_session(ScanSource.FLATBED)
    for blank in (False, True, False):
        multipage.scan_page(sid, lambda b=blank: _jpeg(b))
    assert multipage.get_session_info(sid)["page_count"] == 3
    assert len(PdfReader(BytesIO(multipage.merge_pages(sid))).pages) == 3
    merged = multipage.merge_pages(sid, remove_blank_pages=True)
    assert len(PdfReader(BytesIO(merged)).pages) == 2
    assert multipage.get_page_thumbnail(sid, 0)[:2] == b"\xff\xd8"


def test_scan_page_busy_when_lock_held_elsewhere():
    sid = multipage.create_session(ScanSource.FLATBED)
    held = threading.Event()
    done = threading.Event()

    def holder():
        with scan_lock:
            held.set()
            done.wait(5)

    t = threading.Thread(target=holder)
    t.start()
    held.wait(5)
    called = []
    try:
        with pytest.raises(multipage.ScannerBusyError):
            multipage.scan_page(sid, lambda: called.append(1) or _jpeg(), lock_timeout=0.05)
    finally:
        done.set()
        t.join(5)
    assert called == []
    # lock is free again afterwards
    multipage.scan_page(sid, _jpeg)
    assert multipage.get_session_info(sid)["page_count"] == 1


def test_scan_page_releases_lock_on_failure():
    sid = multipage.create_session(ScanSource.FLATBED)

    def boom():
        raise RuntimeError("scanner offline")

    with pytest.raises(RuntimeError):
        multipage.scan_page(sid, boom)
    assert scan_lock.acquire(blocking=False)
    release_scan()


def test_active_count_and_expiry():
    sid = multipage.create_session(ScanSource.FLATBED)
    assert multipage.active_session_count() == 1
    multipage._sessions[sid].last_activity = datetime.now() - timedelta(minutes=15)
    assert multipage.active_session_count() == 0
    multipage._sessions[sid].last_activity = datetime.now() - timedelta(hours=1)
    assert multipage.get_session(sid) is None


def test_session_cap_evicts_oldest():
    ids = [multipage.create_session(ScanSource.FLATBED) for _ in range(multipage.MAX_SESSIONS + 2)]
    assert len(multipage._sessions) == multipage.MAX_SESSIONS
    assert ids[0] not in multipage._sessions and ids[-1] in multipage._sessions


def test_concurrent_add_page_is_safe():
    sid = multipage.create_session(ScanSource.FLATBED)
    data = multipage.ensure_pdf(_jpeg())
    threads = [threading.Thread(target=multipage.add_page, args=(sid, data)) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert multipage.get_session_info(sid)["page_count"] == 20
