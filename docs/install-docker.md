---
title: Install OpenScanHub with Docker
project: openscan-hub
status: current
tags: [docs, install, docker]
created: 2026-11-06
updated: 2026-11-06
---

# Install OpenScanHub with Docker

Tags: docs, install, docker
Wiki: [[README]]

| Item | Value |
|---|---|
| Version | 0.4.0 |
| Date | 2026-11-06 |
| Standards | ASD-STE100 wording, ISO/IEC/IEEE 26514 layout |

## Purpose

This guide installs OpenScanHub as a Docker container on Linux, a NAS or a
Raspberry Pi.

## Audience

People who run Docker Compose. Images exist for linux/amd64 and linux/arm64.

## Image tags

| Edition | Tags |
|---|---|
| Homelab | `ghcr.io/parallaxintelligencepartnership/openscan-hub:0.4.0` and `:latest` |
| Office | `ghcr.io/parallaxintelligencepartnership/openscan-hub:0.4.0-office` and `:office` |

For the Office edition, also read [Office edition](install-office.md).

## Before you start

- Docker Engine and the Compose plugin are installed.
- You know the time zone name, for example `America/Chicago`.
- You know the scanner IP address, or you can use host networking.

## Steps

1. Make a folder and go to it.

   ```bash
   mkdir openscan-hub && cd openscan-hub
   ```

2. Download the Compose file.

   ```bash
   curl -O https://raw.githubusercontent.com/parallaxintelligencepartnership/openscan-hub/main/docker-compose.yml
   ```

3. Make the scans folder. The container runs as user ID 1000.

   ```bash
   mkdir -p scans
   ```

4. Edit `docker-compose.yml`. Set `TZ` to your time zone.
5. Pin the image tag to `:0.4.0` if you want no surprise updates.

> Caution: To bind-mount a host folder for `/config`, run
> `sudo chown -R 1000:1000 <folder>` first. Without it, nothing saves.

6. To find scanners automatically, use `network_mode: host`. See the comments in the file.
7. Start the container.

   ```bash
   docker compose up -d
   ```

8. Open `http://<your-server>:8020`. Follow the setup wizard.
9. Print the access token when you need it.

   ```bash
   docker exec openscan-hub openscan --show-token
   ```

Scans go to `./scans` (`/scans` in the container). Settings live in the
`openscan-config` volume (`/config`). The image has Tesseract, so OCR works.

## Verification

1. Run `docker ps`. The container must show `healthy`.
2. Open `http://<your-server>:8020/api/health`. It must say `"status": "ok"`.
3. Continue with [feeder to share](setup-feeder-to-share.md).

## Troubleshooting

| Symptom | Cause | Action |
|---|---|---|
| Red warning on the dashboard | `/config` is not writable | Run the `chown` command from the log. |
| Health says `degraded` | Same as above | Fix the folder owner. Restart the container. |
| No scanner found | Bridge network blocks discovery | Use `network_mode: host`, or enter the scanner IP address. |
| File names show the wrong hour | `TZ` is not set | Set `TZ` and restart. |
| No token printed | No config saved yet | Open the web page once. Run the command again. |

## Revision history

| Version | Date | Change |
|---|---|---|
| 0.4.0 | 2026-11-06 | Add Office tags and OCR note. |
