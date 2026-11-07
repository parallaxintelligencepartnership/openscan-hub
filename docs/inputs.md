---
title: OpenScanHub scan inputs
project: openscan-hub
status: current
tags: [docs, inputs, smb, imap]
created: 2026-11-06
updated: 2026-11-06
---

# OpenScanHub scan inputs

Tags: docs, inputs, smb, imap
Wiki: [[README]] · [[protocols]]

| Item | Value |
|---|---|
| Version | 0.4.0 (in development) |
| Date | 2026-11-06 |
| Standards | ASD-STE100 wording, ISO/IEC/IEEE 26514 layout |

## Purpose

This page tells you how to send scans to OpenScanHub from a network share or from a mailbox. It gives one recipe for each input.

## Audience

Home and office users who set up a scanner. Administrators who run a NAS or a mail server.

## Scan to a network folder

A scanner that has "Scan to network folder" (also "Scan to SMB") saves each scan to a shared folder. OpenScanHub picks the file up from there. Two ways exist:

- Folder watch. The share is a folder on the computer that runs OpenScanHub.
- SMB share poll. The share is on a NAS. OpenScanHub logs in to the NAS.

Both ways send the file through the same steps as every other scan: processing, naming and delivery.

### Before you start

- Make a folder for incoming scans. Use a folder that is not your scans folder.
- Make a user on the share that can read, write and delete in that folder.
- Check that the scanner supports SMB 2 or 3. An old scanner can have SMB 1 only. Update its firmware if possible.

> Caution: Do not use the scans folder as the incoming folder. OpenScanHub would find its own output again.

### Way 1: the computer shares the folder, folder watch picks it up

1. Share the incoming folder with the operating system (Windows sharing, macOS File Sharing, or Samba on Linux).
2. On the scanner, enter the computer address, the share name, the user name and the password.
3. In OpenScanHub, open Settings.
4. Turn on "Watch a folder the scanner saves into".
5. Enter the incoming folder path.
6. Save.

### Way 2: the NAS shares the folder, SMB share poll picks it up

1. On the NAS, make a shared folder. Make a user for the scanner.
2. On the scanner, enter the NAS address, the share name, the folder, the user name and the password.
3. In OpenScanHub, open Settings.
4. Turn on "Pick up scans from a network share (NAS)".
5. Enter the server, the share name and the folder inside the share.
6. Enter the user name and the password. Enter the domain only if your network uses one.
7. Choose what happens after delivery: move the file to a `processed` folder, or delete it.
8. Save.

OpenScanHub checks the folder at the interval you set. It takes a file when the size and time stay the same across two checks. Then the file is not still being written.

OpenScanHub writes the name, size and time of each delivered file to a state file in its config folder. It never delivers the same file twice, also after a restart. If the move or delete fails, OpenScanHub tries again but does not deliver the file again.

The poll reads PDF, JPEG, PNG and TIFF files in the one folder. It does not read subfolders.

## Scan to email

A scanner or a phone app sends the scan as an email attachment. OpenScanHub reads the mailbox and takes the attachments.

1. Make a mailbox only for scans.
2. In OpenScanHub, open Settings.
3. Turn on "Take scans from an email mailbox".
4. Enter the mail server, the security (SSL or STARTTLS), the port, the user name and the password.
5. Optional: enter the allowed senders. Use an address or a domain such as `@example.com`. Empty means any sender.
6. Enter the mailbox folder to read. The default is `INBOX`.
7. Enter the folder for finished mail. OpenScanHub makes it if it is not there. Leave it empty to only mark mail as read.
8. Save.

OpenScanHub takes PDF, JPEG, PNG and TIFF attachments from unread mail. After it delivers all attachments of a mail, it marks the mail as read and moves it. If a delivery fails, the mail stays unread and OpenScanHub tries again.

> Caution: The connection checks the server certificate. A server with a self-signed certificate is refused.

Some mail providers need an app password or OAuth. OpenScanHub supports user name and password only.

## Verification

1. Put a PDF in the incoming folder, or send one to the mailbox.
2. Wait for two check intervals.
3. Look for the file in your scans folder.
4. Look at the log if the file does not arrive.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| The log shows a login error for the share | Wrong user name, password or domain | Check the details. Try them from another computer. |
| The poll finds no file | Wrong share name or folder | Check the share name and the folder. Use `/` or `\` between folders. |
| The scanner cannot save to the share | The scanner needs SMB 1 | Update the scanner firmware. Use FTP or email instead. |
| Mail is not read | The sender is not in the allowed list | Add the sender or empty the list. |
| The mail connection fails with a certificate error | The server certificate is not trusted | Use a server with a valid certificate. |

## Revision history

| Date | Change |
|---|---|
| 2026-11-06 | First version. |
