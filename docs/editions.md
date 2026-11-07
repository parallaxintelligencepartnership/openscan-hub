---
title: OpenScanHub editions
project: openscan-hub
status: design
tags: [docs, editions]
created: 2026-11-06
updated: 2026-11-06
---

# OpenScanHub editions

Tags: docs, editions
Wiki: [[README]]

Status: design for 0.4.0. One codebase, two editions, no fork.

## Why two editions

OpenScanHub started as a homelab tool. It has optional Home Assistant and
Paperless-NGX modules. An office does not need them, and code for home tools
can make a business less sure of the product. So the build makes two editions
from the same source.

| | OpenScanHub (Homelab) | OpenScanHub Office |
|---|---|---|
| For | Enthusiasts, hobbyists, homelabs | Offices and small businesses |
| Home Assistant module | Included, off until you turn it on | Not shipped |
| Paperless-NGX destination | Included, off until you turn it on | Not shipped |
| Home Assistant add-on | Yes | No |
| Scan audit log | Optional, off by default | On by default |
| Roles | One access token | Admin token and scan token |
| Outside calls | None | None |
| Scanning, processing, profiles, names, destinations, phone web app | Yes | Yes |

## Names

- Edition ids: `homelab` and `office`.
- Product names: "OpenScanHub" and "OpenScanHub Office".
- Docker: one image, two tag sets.
  - Homelab: `ghcr.io/parallaxintelligencepartnership/openscan-hub:0.4.0` and `:latest`.
  - Office: `ghcr.io/parallaxintelligencepartnership/openscan-hub:0.4.0-office` and `:office`.
- Windows: `OpenScanHub.exe` and `OpenScanHub-Office.exe` on each release.

## Mechanism

The edition is fixed when the package is built. It is not a setting.

1. `openscan/edition.py` reads `EDITION` from `openscan/_edition.py`.
   The file in the repository says `homelab`.
2. An Office build writes `EDITION = "office"` into `openscan/_edition.py`.
3. An Office build also removes the optional modules from the build tree:
   `openscan/integrations/` and `openscan/destinations/paperless.py`.
   The code is not in the image or the EXE, so it cannot run.
4. All code reaches the optional modules through `edition.has(feature)` and a
   lazy import. A missing module is not an error.
5. The settings pages and the API ask `/api/edition` what to show.
   Office hides every Home Assistant and Paperless control.

Build entry points:

- Docker: `docker build --build-arg EDITION=office .`
- Windows: `python build_exe.py --edition office`
- Source: `python -m openscan.edition --set office` before `pip install .`

We chose a build-time edition over a setting because a setting leaves the
home code in the business build. We chose one image with two tag sets over
two image names because one name is easier to find and to document.

## Office defaults

- No outside calls. OpenScanHub never checks for updates or sends usage data
  in either edition. It only connects to the scanners and destinations that
  you set up. A test checks this rule.
- Audit log. Each scan writes one JSON line to `audit.jsonl` in the config
  folder: time, source, profile, page count, file name, destinations, result,
  and who started it (web user role and address, or the scanner button).
  The file rotates at 10 MB and keeps 5 files. Settings shows the last 200
  entries and has a download button.
- Simple admin settings. The admin token opens Settings. The scan token opens
  only the scan page and the profile buttons, so staff phones can scan without
  access to settings.

## Features by module

| Feature | Module | Homelab | Office |
|---|---|---|---|
| `homeassistant` | `openscan/integrations/homeassistant.py` | yes | no |
| `paperless` | `openscan/destinations/paperless.py` | yes | no |
| `audit_default_on` | `openscan/audit.py` | no | yes |
| `scan_role` | `openscan/web/server.py` | optional | yes |

## Migration

A Homelab install keeps its settings. An Office build that finds Home
Assistant or Paperless settings in an old config file ignores them, logs one
line, and keeps them in the file.

## Not in scope

- A second repository or a fork.
- Paid features or license keys.
- Code signing. Options are listed in the release notes.
