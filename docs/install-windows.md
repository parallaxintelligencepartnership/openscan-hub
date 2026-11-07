---
title: Install OpenScanHub on Windows
project: openscan-hub
status: current
tags: [docs, install, windows]
created: 2026-11-06
updated: 2026-11-06
---

# Install OpenScanHub on Windows

Tags: docs, install, windows
Wiki: [[README]]

| Item | Value |
|---|---|
| Version | 0.4.0 |
| Date | 2026-11-06 |
| Standards | ASD-STE100 wording, ISO/IEC/IEEE 26514 layout |

## Purpose

This guide installs OpenScanHub as a Windows program. It runs in the system tray.

## Audience

Windows users in a home or small office.

## Files

| Edition | File |
|---|---|
| Homelab | `OpenScanHub.exe` |
| Office | `OpenScanHub-Office.exe` (see [Office edition](install-office.md)) |

## Steps

1. Download the EXE and `SHA256SUMS.txt` from the latest release page.
2. Open PowerShell in the Downloads folder.
3. Run this command.

   ```powershell
   Get-FileHash .\OpenScanHub.exe -Algorithm SHA256
   ```

4. Compare the number with the line for the file in `SHA256SUMS.txt`.

> Caution: If the numbers differ, delete the file and download it again.

5. Double-click the EXE. Your browser opens the setup page.
6. If Windows shows **Windows protected your PC**, press **More info**, then **Run anyway**. The file is not code-signed yet.
7. Follow the setup wizard.
8. Right-click the tray icon to scan, open the dashboard, or show your access token.

## Install Tesseract for OCR

OpenScanHub does not ship Tesseract on Windows. OCR needs it.

1. Install Tesseract for Windows. Use the standard install folder.
2. Restart OpenScanHub.
3. Turn on **Recognize text** in a profile.

Without Tesseract, a scan still saves. A warning says text recognition was skipped.

## Verification

1. Open `http://localhost:8020`. The dashboard must show your scanner.
2. Continue with [feeder to share](setup-feeder-to-share.md).

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| The browser does not open | A browser setting blocks it | Open `http://localhost:8020` yourself. |
| Windows Firewall asks a question | Phones and scanners need access | Allow private networks. |
| The phone cannot open the page | The address is `localhost` | Open the dashboard by the computer's network address. |
| Warning about Tesseract | Not installed | Install it and restart. |
| A second copy does not start | OpenScanHub runs one copy | Use the tray icon of the first copy. |

## Revision history

| Version | Date | Change |
|---|---|---|
| 0.4.0 | 2026-11-06 | First version. |
