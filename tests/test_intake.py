"""intake.handle_scan end to end: process, split, name, deliver, queue, audit."""

import json
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image, ImageDraw
from pypdf import PdfReader, PdfWriter

from openscan import _edition, audit, intake, naming, processing, retry_queue
from openscan.config import AppConfig
from openscan.processing import ProcessOptions, ProcessResult, registry
from openscan.profiles import Profile


def _pdf(*blanks) -> bytes:
    frames = []
    for blank in blanks:
        img = Image.new("RGB", (600, 800), "white")
        if not blank:
            ImageDraw.Draw(img).rectangle((100, 100, 500, 700), fill="black")
        frames.append(img)
    buf = BytesIO()
    frames[0].save(buf, format="PDF", save_all=True, append_images=frames[1:])
    return buf.getvalue()


def _pages(path) -> int:
    return len(PdfReader(str(path)).pages)


@pytest.fixture
def queue(tmp_path, monkeypatch):
    q = retry_queue.RetryQueue(tmp_path / "q" / "retry_queue.json", tmp_path / "q" / "files")
    monkeypatch.setattr(retry_queue, "_queue", q)
    yield q
    q.stop()


@pytest.fixture
def cfg(tmp_path, config_dir, queue):
    c = AppConfig()
    c.output.folder = str(tmp_path / "out")
    return c


@pytest.fixture
def isolated(monkeypatch):
    monkeypatch.setattr(registry, "_stages", dict(registry._stages))
    monkeypatch.setattr(intake, "_splitters", dict(intake._splitters))
    monkeypatch.setattr(naming, "_providers", [])


def test_default_profile_end_to_end_to_a_folder(cfg, tmp_path):
    cfg.scan.remove_blank_pages = True
    cfg.output.filename_pattern = "doc_{date:%Y}_{n}"
    cfg.audit.enabled = True
    res = intake.handle_scan(cfg, _pdf(False, True, False), source="adf", started_by="web 1.2.3.4")
    assert res.ok and not res.errors and not res.queued
    [saved] = res.saved_paths
    assert Path(saved).parent == tmp_path / "out"
    assert res.filename == Path(saved).name and res.filename.startswith("doc_2")
    assert res.filename.endswith("_001.pdf")
    assert _pages(saved) == 2 and res.page_count == 2
    assert res.profile_id == "default"
    [event] = audit.recent(5)
    assert event["source"] == "adf" and event["pages"] == 2 and event["result"] == "ok"
    assert event["file"] == res.filename and event["destinations"] == ["folder"]
    assert event["started_by"] == "web 1.2.3.4" and event["profile"] == "default"


def test_audit_off_by_default_in_homelab(cfg, config_dir):
    intake.handle_scan(cfg, _pdf(False), source="flatbed")
    assert not (config_dir / "audit.jsonl").exists()


def test_explicit_profile_name_rule_and_destinations(cfg, tmp_path):
    other = tmp_path / "invoices"
    profile = Profile(id="inv", name="Invoices", name_rule="{profile}_{source}_{n}",
                      destinations=[{"type": "folder", "options": {"folder": str(other)}}])
    res = intake.handle_scan(cfg, _pdf(False, True), source="flatbed", profile=profile)
    assert res.ok and res.filename == "Invoices_flatbed_001.pdf"
    assert _pages(other / res.filename) == 2  # blank removal is off in this profile
    assert not (tmp_path / "out").exists()


def test_stage_failure_is_a_warning_and_the_scan_is_saved(cfg, isolated):
    def broken(pdf, opts, result):
        raise RuntimeError("deskew exploded")
    processing.register_stage("deskew", 15, broken, lambda o: o.deskew)
    profile = Profile(id="p", name="P", processing=ProcessOptions(deskew=True),
                      destinations=[{"type": "folder", "options": {}}])
    data = _pdf(False)
    res = intake.handle_scan(cfg, data, source="adf", profile=profile)
    assert res.ok and "deskew exploded" in res.warnings[0]
    assert Path(res.saved_paths[0]).read_bytes() == data


def test_splitter_makes_several_documents(cfg, isolated):
    def per_page(pdf, result):
        reader = PdfReader(BytesIO(pdf))
        out = []
        for page in reader.pages:
            w = PdfWriter()
            w.add_page(page)
            buf = BytesIO()
            w.write(buf)
            out.append((buf.getvalue(), ProcessResult(pdf=buf.getvalue())))
        return out
    intake.register_splitter("blank", per_page)
    profile = Profile(id="s", name="S", split="blank", name_rule="part_{n}",
                      destinations=[{"type": "folder", "options": {}}])
    res = intake.handle_scan(cfg, _pdf(False, False, False), source="adf", profile=profile)
    assert res.ok and [d.filename for d in res.documents] == ["part_001.pdf", "part_002.pdf",
                                                             "part_003.pdf"]
    assert res.page_count == 3 and len(res.saved_paths) == 3


def test_missing_splitter_keeps_one_document(cfg, isolated):
    profile = Profile(id="b", name="B", split="barcode", destinations=[{"type": "folder", "options": {}}])
    res = intake.handle_scan(cfg, _pdf(False, False), source="adf", profile=profile)
    assert res.ok and len(res.documents) == 1
    assert any("barcode" in w for w in res.warnings)


def test_field_provider_feeds_the_name(cfg, isolated):
    naming.register_field_provider(lambda result, ctx: {"vendor": f"ACME-{len(result.pdf) > 0}"})
    profile = Profile(id="v", name="V", name_rule="{vendor}_{doc_date}",
                      destinations=[{"type": "folder", "options": {}}])
    res = intake.handle_scan(cfg, _pdf(False), source="adf", profile=profile)
    assert res.filename == "ACME-True_unknown.pdf"


def test_dropped_file_keeps_name_and_bytes_without_processing(cfg, tmp_path):
    jpeg = BytesIO()
    Image.new("RGB", (50, 50), "white").save(jpeg, format="JPEG")
    res = intake.handle_scan(cfg, jpeg.getvalue(), source="ftp", filename="photo.jpg")
    assert res.ok and res.filename == "photo.jpg"
    assert (tmp_path / "out" / "photo.jpg").read_bytes() == jpeg.getvalue()


def test_dropped_image_becomes_pdf_when_processing_is_on(cfg, tmp_path):
    cfg.scan.remove_blank_pages = True
    jpeg = BytesIO()
    img = Image.new("RGB", (300, 400), "white")
    ImageDraw.Draw(img).rectangle((50, 50, 250, 350), fill="black")
    img.save(jpeg, format="JPEG")
    res = intake.handle_scan(cfg, jpeg.getvalue(), source="ftp", filename="photo.jpg")
    assert res.ok and res.filename == "photo.pdf"
    assert _pages(tmp_path / "out" / "photo.pdf") == 1


def test_queue_uses_the_source_file_only_for_unchanged_bytes(cfg, tmp_path, queue):
    blocker = tmp_path / "out"
    blocker.write_text("not a folder")
    src = tmp_path / "staged.pdf"
    data = _pdf(False, True)
    src.write_bytes(data)
    res = intake.handle_scan(cfg, data, source="ftp", filename="staged.pdf",
                             source_path=str(src), source_owned=True)
    assert res.queued and res.uses_source
    assert queue.pending()[0]["file"] == str(src)

    cfg.scan.remove_blank_pages = True  # bytes change, so the queue keeps a copy
    res = intake.handle_scan(cfg, data, source="ftp", filename="staged.pdf",
                             source_path=str(src), source_owned=True)
    assert res.queued and not res.uses_source
    copy = queue.pending()[1]["file"]
    assert copy != str(src) and len(PdfReader(copy).pages) == 1


def test_office_skips_paperless_destinations(cfg, tmp_path, monkeypatch):
    monkeypatch.setattr(_edition, "EDITION", "office")
    cfg.paperless.enabled, cfg.paperless.mode = True, "consume"
    cfg.paperless.consume_folder = str(tmp_path / "consume")
    profile = Profile(id="o", name="O", destinations=[
        {"type": "paperless_consume", "options": {"folder": str(tmp_path / "c2")}},
        {"type": "folder", "options": {}}])
    res = intake.handle_scan(cfg, _pdf(False), source="adf", profile=profile)
    assert res.ok and len(res.saved_paths) == 1
    assert not (tmp_path / "c2").exists()
    res = intake.handle_scan(cfg, _pdf(False), source="adf")
    assert res.ok and not (tmp_path / "consume").exists()


def test_no_destination_is_reported(cfg):
    cfg.output.folder = ""
    res = intake.handle_scan(cfg, _pdf(False), source="adf")
    assert res.ok is False and res.errors and "No destination" in res.errors[0]


def test_result_reads_like_a_delivery_result(cfg):
    res = intake.handle_scan(cfg, _pdf(False), source="adf")
    assert json.loads(json.dumps(res.to_dict())) == {
        "ok": True, "saved_paths": res.saved_paths, "errors": [], "queued": False}
