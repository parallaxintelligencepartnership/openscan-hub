# Lane B needs (for the orchestrator to merge)

## Lane A: openscan/web/dashboard_api.py

1. Use the global scanner lock instead of the module `_scan_lock`/`_scan_in_progress`
   so manual scans and the ADF monitor exclude each other.

```python
from ..scanner.base import try_acquire_scan, release_scan

# in _do_scan, replace the `with _scan_lock: if _scan_in_progress ...` block and the finally:
    if not try_acquire_scan(2.0):
        handler._send_json({"error": "The scanner is busy with another scan. Try again in a moment."}, status=409)
        return
    try:
        ...  # unchanged body
    finally:
        release_scan()
```

2. `_execute_scan`: use the saved scan defaults. Protocols now return one finished PDF
   (every feeder page, blank pages removed when asked).

```python
    d = config.scan
    settings = ScanSettings(source=source, resolution=d.resolution, color_mode=d.color_mode,
                            page_size=d.page_size, duplex=d.duplex,
                            remove_blank_pages=d.remove_blank_pages)
```

3. `/api/multipage/scan`: per-page lock with a clear busy error.

```python
    try:
        page_count = multipage.scan_page(session_id, lambda: _execute_scan(session.source))
    except multipage.ScannerBusyError as e:
        handler._send_json({"success": False, "error": str(e)}, status=409)
        return
    except Exception as e:
        logger.exception("Multipage scan failed")
        handler._send_json({"success": False, "error": str(e)}, status=500)
        return
    handler._send_json({"success": True, "page_count": page_count, "page_index": page_count - 1})
```
   (`size_bytes` of the single page is no longer returned; the UI does not need it.)

4. `/api/multipage/save`:
   `pdf_data = multipage.merge_pages(session_id, remove_blank_pages=config.scan.remove_blank_pages)`

5. After settings are saved (and after the wizard finishes): `from ..monitor import start_monitor; start_monitor(config)`.
   It is idempotent, starts/stops according to `config.monitor.enabled`, and picks up scanner changes.
   For the dashboard "scan in progress" flag keep your own boolean set inside the locked section.

## Lane A: openscan/web/wizard_api.py (test scan)

Wrap the test scan in the same lock:
```python
    if not try_acquire_scan(2.0):
        return handler._send_json({"success": False, "error": "The scanner is busy. Try again in a moment."}, status=409)
    try:
        data = impl.scan(scanner_info, settings)   # returns a real PDF now
    finally:
        release_scan()
```

## openscan/main.py (owner per orchestrator)

`start_monitor` honors `config.monitor.enabled` itself, so:
```python
    if config.wizard_completed:
        from .monitor import start_monitor
        start_monitor(config)
```
and on shutdown: `from .monitor import stop_monitor; stop_monitor()`.

## Lane C: pyproject.toml / requirements.txt / build_exe.py

PyMuPDF is no longer imported anywhere (thumbnails use pypdfium2 via openscan/pages.py).
Remove `PyMuPDF` from dependencies, requirements.txt, the Dockerfile (PyMuPDF build/apt lines) and any PyInstaller hidden imports/collect
entries; keep the `pypdfium2` pin you are adding.
