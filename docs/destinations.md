---
title: OpenScanHub destinations
project: openscan-hub
status: current
tags: [docs, destinations]
created: 2026-11-06
updated: 2026-11-06
---

# OpenScanHub destinations

Tags: docs, destinations
Wiki: [[README]] · [[architecture]]

| Item | Value |
|---|---|
| Version | 0.4.0 (in development) |
| Date | 2026-11-06 |
| Standards | ASD-STE100 wording, ISO/IEC/IEEE 26514 layout |

## Purpose

This page lists each place OpenScanHub can send a finished scan. It lists the
fields of each destination. It also explains why cloud drives are not built in.

## Audience

People who set up profiles, and developers who add a destination.

## Destinations

Set a destination in a profile. Press Test to check the settings. A test never
sends a scan. A failed delivery goes to the retry queue. Passwords are never
written to the retry queue or to the log.

### Folder or network share (`folder`)

| Field | Meaning |
|---|---|
| Folder | A local folder or a mounted network share. |

### Paperless-ngx (`paperless_consume`, `paperless_api`)

Homelab edition only. See the Paperless fields in Settings.

### SMB network share (`smb`)

OpenScanHub writes to the share itself. You do not mount it.

| Field | Meaning |
|---|---|
| Server name or address | The host name or IP address. Add `:port` for a port other than 445. |
| Share name | The name of the share. |
| Folder on the share | An optional folder. OpenScanHub creates missing folders. |
| User name | The account on the server. |
| Password | The password of the account. |
| Domain or workgroup | Optional. OpenScanHub sends `DOMAIN\user`. |

A file name that is already in use gets a counter (`_002`, or `_2`).
The Test button writes a small file and deletes it.

### WebDAV or Nextcloud (`webdav`)

| Field | Meaning |
|---|---|
| WebDAV address | The full address, for example `https://cloud.example.com/remote.php/dav/files/USER`. |
| Folder | An optional folder. OpenScanHub creates missing folders. |
| User name | The account name. |
| Password or app password | Use an app password if the server has two-factor sign-in. |
| Allow a self-signed certificate | Off by default. Certificates are checked unless you turn it on. |

OpenScanHub never overwrites a file. It does not follow redirects, so use the
exact address. The Test button sends a PROPFIND request.

### Email (`email`)

| Field | Meaning |
|---|---|
| Mail server | The SMTP host name. |
| Port | Default 587 (starttls), 465 (ssl) or 25 (none). |
| Security | `starttls`, `ssl` or `none`. |
| User name | Optional. |
| Password | Optional. |
| From address | The sender address. |
| Send to | One or more addresses. |
| Subject | A template. Use `{filename}`, `{date}` and `{time}`. |
| Largest attachment (MB) | Default 20. |

If a scan is larger than the limit, delivery fails with a plain message. The
retry queue keeps the scan. The Test button signs in and sends nothing.

## Cloud drives

Google Drive, OneDrive and Dropbox are not built in. Each one needs an OAuth
app, and each user must register that app with the provider. OpenScanHub does
not ship shared keys.

| Service | Cost to register an app | Note |
|---|---|---|
| Dropbox | Free | Needs a Dropbox app and an access token. |
| Google Drive | Free for personal use | A public app needs a Google review. |
| OneDrive | Free | Needs an app registration in Microsoft Entra. |

Check each provider's current terms before you rely on this table.

Nextcloud works now through the WebDAV destination. Many other cloud services
also offer WebDAV. For other drives, sync a drive client to a folder and use
the folder destination.

## Add a destination

See [[architecture]]. Write a module in `openscan/destinations/`, add the
module name to `_MODULES`, and write tests against a real server where you can.

## Troubleshooting

| Symptom | Action |
|---|---|
| "rejected the user name or password" | Check the account. Use an app password for WebDAV and email. |
| "certificate ... is not trusted" | Install a valid certificate, or turn on the self-signed option. |
| "Could not reach" | Check the host name, the port and the network. |
| Email "above the limit" | Raise the size limit, or use another destination. |

## Revision history

| Version | Date | Change |
|---|---|---|
| 0.4.0 | 2026-11-06 | Added smb, webdav and email. |
