---
title: OpenScanHub architecture
project: openscan-hub
status: current
tags: [docs, architecture]
created: 2026-11-06
updated: 2026-11-06
---

# OpenScanHub architecture

Tags: docs, architecture
Wiki: [[README]] · [[editions]]

| Item | Value |
|---|---|
| Version | 0.4.0 (in development) |
| Date | 2026-11-06 |
| Standards | ASD-STE100 wording |

## Purpose

This page tells you how a finished scan moves through OpenScanHub. It also
tells you how to add a processing stage, a destination, a name field or a
splitter.

## Audience

Developers who change OpenScanHub.

## The intake flow

Every finished scan goes through one function:
`openscan.intake.handle_scan`. The sources are the dashboard, the feeder
monitor, the FTP receiver and the folder watcher. The eSCL and WSD drivers
only join pages into one PDF. They do not remove blank pages.

```
handle_scan(config, data, source=..., profile=None, started_by="",
            source_path=None, source_owned=False, filename=None) -> IntakeResult

1. Profile     profiles.get(config) when profile is None
2. Ensure PDF  pages.ensure_pdf(data)
3. Process     processing.run(pdf, profile.processing) -> ProcessResult
4. Split       splitter for profile.split -> [(pdf, ProcessResult), ...]
5. Per document:
   a. Name     naming.render(profile.name_rule, naming.collect_fields(...))
   b. Deliver  destinations.deliver_all(pdf, name, targets)
               failures go to the retry queue
   c. Audit    audit.record(...) when config.audit.enabled
```

Rules:

- `IntakeResult` has `ok`, `saved_paths`, `errors` and `queued` for all
  documents, the same fields as `DeliveryResult`. It also has `filename`,
  `size_bytes`, `page_count`, `warnings` and `documents`.
- A file from FTP or folder watch keeps its name (`filename=`). If it is not
  a PDF and no stage is on, OpenScanHub delivers the bytes unchanged.
- The retry queue retries from `source_path` only while the bytes are the
  original bytes. After processing changes them, the queue keeps a copy.
- The queue file never holds secrets. A retry gets them from the config.

## Profiles

`openscan/profiles.py` holds the `Profile` dataclass. The config holds
`profiles` and `default_profile_id`.

An old config gets one "Default" profile with `follow_settings=True`. At
scan time, `profiles.get()` builds that profile again from the old Settings
sections. Thus the Settings page works as before. A profile editor must set
`follow_settings=False` when the user changes the profile.

A destination entry is `{"type": ..., "options": {...}}`. An empty or
missing option uses the value from the old Settings sections, for example
the scans folder or the Paperless-NGX token.

## Editions

`openscan.edition.has(feature)` tells you if this build has a feature. See
[[editions]]. An Office build deletes `openscan/integrations/` and
`openscan/destinations/paperless.py`. Code must reach optional modules
through `edition.has()` and an import that can fail.

`GET /api/edition` returns `{"edition": ..., "features": {...}}`.

## Add a processing stage

1. Make a new module in `openscan/processing/`, for example `deskew.py`.
2. Write the stage function: `fn(pdf: bytes, options, result) -> bytes`.
3. Call `register_stage(name, order, fn, enabled)` at the module level.
   `enabled` is `lambda o: o.deskew`.
4. Add the module name to `_STAGES` in `openscan/processing/__init__.py`.

Order values: 10 auto_rotate, 15 deskew, 20 crop, 30 remove_blank_pages,
40 output_color, 50 ocr, 60 compression, 70 pdfa.

Caution: import optional libraries inside the stage function, not at the
module level. If the library is missing, raise `StageUnavailable` with a
plain sentence. Any exception adds a warning, and the scan continues with
the bytes from before the stage.

An OCR stage puts one string per page in `result.page_texts`. A stage that
removes pages adds the number to `result.pages_removed`.

## Add a destination

1. Make a new module in `openscan/destinations/`, for example `smb.py`.
2. Subclass `Destination`. Set `type`, `label`, `feature` (or `None`),
   `target_key` and `FIELDS`. Make each field with
   `option_field(key, label, kind, required=..., default=..., choices=...)`.
   The kinds are text, password, path, bool, choice and list.
3. Write `deliver(pdf, filename, options) -> (location, confirmed)`. Raise
   `DeliveryError` with a plain sentence when it fails.
4. Write `test(options) -> {"ok": bool, "message": str}`.
5. Add the module name to `_MODULES` in `openscan/destinations/__init__.py`.

`destinations.available()` lists the types that this edition has. The
registry finds each `Destination` subclass in a listed module. It skips a
module that it cannot import. A field of kind `password` is not written to
the retry queue and is blanked in `GET /api/config`.

## Add a name field

Register a provider in `openscan/naming.py`:

```python
from openscan.naming import register_field_provider

@register_field_provider
def ocr_fields(result, context):
    return {"doc_date": some_date, "title": "Invoice 42", "vendor": "ACME"}
```

`result` is the `ProcessResult` of the document. `context` has `config`,
`profile`, `source`, `started_by`, `now` and `n`. A provider cannot replace
`date`, `time`, `n`, `profile` or `source`. A `date` or `datetime` value
takes a format, for example `{doc_date:%Y-%m}`. A missing or empty field
becomes `unknown`. Import the provider module from the module that needs it,
for example the OCR stage.

## Add a splitter

```python
from openscan.intake import register_splitter

register_splitter("blank", split_on_blank_pages)
```

The function gets `(pdf, process_result)` and returns a list of
`(pdf, ProcessResult)`, one item for each document. If the splitter is not
registered, returns nothing or raises, the scan stays one document and gets
a warning.

## Audit log

`openscan/audit.py` writes one JSON line for each delivered document to
`audit.jsonl` in the config folder. The file rotates at 10 MB and keeps 5
old files. `audit.recent(n)` returns the newest entries first.
`config.audit.enabled` is on by default in the Office edition only.

## Next feed profile

Paper goes in the document feeder and the feeder monitor
(`openscan/monitor.py`) scans it. A user picks the profile for the next
batch on the phone page `/m` or on the dashboard.

- `openscan/feed.py` holds the choice in memory. A restart clears it.
- `POST /api/feed/next` with `{"profile_id": id}` sets it. `{"profile_id":
  null}` clears it.
- `GET /api/feed` returns `next_profile` (`{id, name}` or null),
  `expires_in_seconds`, `adf_state`, `scanner_state`, `monitor_enabled` and
  `last_scans` (the last five, each with `file`, `time`, `profile`, `result`).
- The choice is one-shot. It clears after the monitor delivers that batch,
  or after 10 minutes (`feed.TTL_SECONDS`).
- The monitor uses the chosen profile for the scanner settings and for
  `intake.handle_scan(profile=...)`. Without a choice it uses the default
  profile. The audit event holds the profile name and `started_by` stays
  `feeder monitor`.
- Both routes are in the scan-role allow list in `openscan/web/server.py`.
- When the monitor is off, a dashboard profile button scans at once with
  `POST /api/profiles/<id>/scan`.

## Old interfaces

`openscan/output.py` keeps `save_scan`, `save_scan_for_config`,
`deliver_to`, `generate_filename` and the Paperless-NGX helpers as thin
wrappers. New code uses `intake.handle_scan` or `destinations`.

## Revision history

| Date | Change |
|---|---|
| 2026-11-06 | Add the next feed profile. |
| 2026-11-06 | First version: intake, profiles, destinations, processing, naming, audit. |
