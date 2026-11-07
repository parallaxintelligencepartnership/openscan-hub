---
title: OpenScanHub glossary
project: openscan-hub
status: current
tags: [docs, glossary]
created: 2026-11-06
updated: 2026-11-06
---

# OpenScanHub glossary

Tags: docs, glossary
Wiki: [[README]]

| Item | Value |
|---|---|
| Version | 0.4.0 |
| Date | 2026-11-06 |
| Standards | ASD-STE100 wording, ISO/IEC/IEEE 26514 layout |

## Purpose

This page defines the terms in the OpenScanHub guides.

## Audience

All users.

## Terms

| Term | Meaning |
|---|---|
| ADF | Automatic document feeder. The tray that feeds paper to the scanner. |
| Admin token | The secret that opens all pages, including Settings. |
| Audit log | A file with one line for each scan (`audit.jsonl`). |
| Auto-rotate | A step that turns pages upright. |
| Deskew | A step that straightens a tilted page. |
| eSCL | The AirScan protocol. Many network scanners use it. |
| Feeder monitor | A task that watches the feeder and starts a scan when paper appears. |
| Destination | A place that gets the finished file: folder, SMB, WebDAV, email or Paperless-NGX. |
| Next feed | The profile you picked for the next batch. It ends after one batch or 10 minutes. |
| OCR | Text recognition. It makes a searchable PDF. |
| PDF/A | A PDF type for long-term storage. |
| Profile | One saved set of scanner, clean-up, name and destination settings. |
| Retry queue | A list of failed deliveries that OpenScanHub sends again. |
| Scan token | A secret that opens only the scan pages. For staff phones. |
| Separator page | A blank sheet, or a printed `OSH:` QR sheet, that splits a batch. |
| Smart name | A file name built from text on the page, such as `{vendor}`. |
| SMB | The Windows file sharing protocol. |
| Tesseract | A free OCR program. The Docker image has it. Windows users install it. |
| WebDAV | A web file protocol. Nextcloud uses it. |
| WSD | Web Services for Devices. A scan protocol with no feeder monitor in OpenScanHub. |

## Revision history

| Version | Date | Change |
|---|---|---|
| 0.4.0 | 2026-11-06 | First version. |
