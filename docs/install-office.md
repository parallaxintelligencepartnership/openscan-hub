---
title: Set up the Office edition
project: openscan-hub
status: current
tags: [docs, install, office]
created: 2026-11-06
updated: 2026-11-06
---

# Set up the Office edition

Tags: docs, install, office
Wiki: [[README]]

| Item | Value |
|---|---|
| Version | 0.4.0 |
| Date | 2026-11-06 |
| Standards | ASD-STE100 wording, ISO/IEC/IEEE 26514 layout |

## Purpose

This guide sets up OpenScanHub Office. It adds a scan token for staff phones
and an audit log. Install first with [install with Docker](install-docker.md) or [install on Windows](install-windows.md).

## Audience

Office administrators.

## What the Office edition has

- An admin token. It opens everything, including Settings.
- A scan token. It opens only the dashboard, the phone page and the profile buttons.
- An audit log. It is on by default.
- No outside calls. A test checks this rule.

## What the Office edition does not ship

- The Home Assistant module and the Home Assistant add-on.
- The Paperless-NGX destination.

The code is not in the image or the EXE. An old config with those settings keeps them but ignores them.

## Steps

1. Use the Office image tag `:0.4.0-office` or `:office`. On Windows, use `OpenScanHub-Office.exe`.
2. Open the dashboard. Sign in with the admin token.
3. Print the scan token.

   ```bash
   docker exec openscan-hub openscan --show-token --scan
   ```

   On Windows, run `openscan --show-token --scan` or use the tray menu.

> Caution: Give the scan token to staff only. Never give the admin token to staff.

4. On the dashboard, find **Scan from your phone**. Show the QR code to staff.
5. Press **Show phone code** to add the scan token to the code. Show it to staff only.
6. Staff open the page and pick a profile. Then they load the paper.
7. Open **Settings**. Find **Scan audit log**.
8. Check that the record is on.

The log is `audit.jsonl` in the config folder. Each scan adds one line: time,
source, profile, page count, file name, destinations, result and who started it.
The file rotates at 10 MB and keeps 5 files. Settings shows the last 200
entries. Press **Download audit.jsonl** to save it.

Phones can install the page as an app only over HTTPS. Over plain HTTP, the page works in the browser.

## Verification

1. Open the phone page with the scan token. The role note must say **Scan only**.
2. Open `/settings` with the scan token. The answer must be a refusal.
3. Run one scan. A new line must show in the audit log.

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| Staff see a refusal | They use the scan token on an admin page | Use the dashboard or `/m`. |
| The QR code opens `localhost` | You opened the dashboard by `localhost` | Open it by the server's network address. |
| No Home Assistant or Paperless controls | Office does not ship them | Use the Homelab edition for them. |
| The audit list is empty | No scan ran yet | Run a scan. |
| The token is lost | Not saved | Run the show-token command again. |

## Revision history

| Version | Date | Change |
|---|---|---|
| 0.4.0 | 2026-11-06 | First version. |
