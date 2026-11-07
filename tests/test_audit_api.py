"""Audit viewer endpoints and the switch."""

from conftest import TOKEN

from openscan import audit


def test_audit_lists_the_last_200_newest_first(live_server, app_config):
    app_config.audit.enabled = True
    for i in range(205):
        audit.record({"source": "ftp", "i": i})
    r = live_server.get("/api/audit", token=TOKEN)
    data = r.json()
    assert r.status == 200 and data["enabled"] is True
    assert len(data["entries"]) == 200 and data["entries"][0]["i"] == 204


def test_audit_empty_and_download(live_server):
    assert live_server.get("/api/audit", token=TOKEN).json()["entries"] == []
    empty = live_server.get("/api/audit/download", token=TOKEN)
    assert empty.status == 200 and empty.body == b""
    audit.record({"source": "adf", "file": "a.pdf"})
    r = live_server.get("/api/audit/download", token=TOKEN)
    assert r.status == 200
    assert "audit.jsonl" in r.headers["Content-Disposition"]
    assert b'"file": "a.pdf"' in r.body


def test_audit_needs_the_token(live_server):
    assert live_server.get("/api/audit").status == 401
    assert live_server.get("/api/audit/download").status == 401


def test_the_switch_saves_through_settings(live_server, app_config):
    assert live_server.post("/api/settings", {"audit": {"enabled": True}}, token=TOKEN).status == 200
    assert live_server.get("/api/audit", token=TOKEN).json()["enabled"] is True
    assert live_server.post("/api/settings", {"audit": {"enabled": False}}, token=TOKEN).status == 200
    assert live_server.get("/api/audit", token=TOKEN).json()["enabled"] is False


def test_every_intake_caller_passes_started_by():
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent / "openscan"
    for rel in ("monitor.py", "scanner/ftp_receive.py", "scanner/folder_watch.py"):
        text = (root / rel).read_text(encoding="utf-8")
        calls = re.findall(r"intake\.handle_scan\((.*?)\)\n", text, re.S)
        assert calls and all("started_by=" in c and 'started_by=""' not in c for c in calls), rel
    web = (root / "web" / "dashboard_api.py").read_text(encoding="utf-8")
    assert "started_by=started_by" not in web.split("def save_document")[0]
    assert "web_started_by(handler)" in web
