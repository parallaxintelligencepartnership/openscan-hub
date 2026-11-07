---
title: Scan protocols OpenScanHub could add
project: openscan-hub
status: draft
tags: [openscan-hub, scanning, protocols, research]
created: 2026-11-06
updated: 2026-11-06
---

# Scan protocols OpenScanHub could add

Tags: #openscan-hub #scanning #protocols #research
Wiki: [[OpenScanHub]]

Research date: 2026-11-06. Version this applies to: 0.4.0.

This document lists the ways a scan can reach OpenScanHub, beyond what it supports now. For each protocol, it gives the reach and the spec status. It also gives the libraries and their licenses, a test plan without hardware, the build cost and a recommendation.

Rules for this document:

- Every fact has a source link that was opened during the research.
- "Not verified" marks a claim that no opened source confirmed.
- "Judgment" marks an opinion or estimate, not a fact.
- Build cost: S is days, M is one to two weeks, L is more (Judgment).

## What OpenScanHub supports today

| Protocol | Direction | Module | Notes |
|---|---|---|---|
| eSCL (AirScan, Mopria) | Pull: OpenScanHub asks the scanner for a scan | `openscan/scanner/escl.py` | Mopria Scan uses eSCL as a core part, so Mopria scanners are covered. |
| WSD Scan (WS-Scan) | Pull | `openscan/scanner/wsd.py` | Discovery, status, capabilities, CreateScanJob and RetrieveImage. No event subscription: the code has no `Subscribe` or `ScanAvailableEvent` handling. A scan started at the scanner panel does not reach OpenScanHub over WSD. |
| FTP receiver | Push: the scanner sends the file | `openscan/scanner/ftp_receive.py` | Built on pyftpdlib. The scanner uses its "Scan to FTP" setting. Default port 2121. |
| Folder watch | Push, through any tool that writes a file | `openscan/scanner/folder_watch.py` | Watches a folder for PDF, JPEG, TIFF and PNG files. |
| SMB share poll | Push, through a share on a NAS | `openscan/scanner/smb_poll.py` | Uses smbprotocol. The scanner uses "Scan to network folder". OpenScanHub logs in to the share and polls one folder. See [inputs.md](inputs.md). |
| Email in (IMAP) | Push, through a mailbox | `openscan/scanner/imap_receive.py` | Uses the Python standard library. Takes PDF and image attachments from unread mail. See [inputs.md](inputs.md). |

Sources: the repository code. Mopria: https://www.mopria.org/specifications

## Ranked list

| Rank | Protocol | Reach | Spec | Library license vs MIT | Cost | Recommendation |
|---|---|---|---|---|---|---|
| 1 | Scan to SMB, by watching a share | Very high | Open (SMB) | No new library for a local share. smbprotocol (MIT) for a remote share. | S (recipe), M (remote poll) | Built in 0.4.0 (recipe and remote poll) |
| 2 | Phone camera upload in the web app | Every user with a phone | HTML standard | No new library | S | Build in 0.4.0 |
| 3 | Email in (IMAP mailbox poll) | High | Open (IMAP) | Python standard library | M | Built in 0.4.0 |
| 4 | WSD ScanAvailableEvent push | Medium to high (WSD devices) | Published by Microsoft | No new library | M to L | Later (0.5) |
| 5 | WebDAV receiver | Low to medium (office MFPs) | Open | WsgiDAV (MIT) | S to M | Later |
| 6 | SANE network client (saned) | Low (Linux users with a USB scanner on a server) | Public domain | Write our own client | M to L | Later |
| 7 | Brother scan key push (brscan-skey) | Medium (Brother only) | No public spec | Open projects are GPL-3.0 | L | Later, only with demand |
| 8 | TWAIN or WIA on Windows | Windows desktop users | TWAIN open, WIA Microsoft | pytwain is GPLv2; pywin32 is PSF | L | Later, only with demand |
| 9 | Embedded SMB server (impacket) | Same as rank 1 | Open | Modified Apache 1.1 with credit clause | M plus risk | Never in its current state |
| 10 | IPP Scan (PWG 5100.17) | No known devices | Open | pyipp (MIT) has no scan support | L | Never, unless devices ship |
| 11 | Vendor push (Epson, Canon, ScanSnap apps) | Medium | Closed | Not applicable | L | Never; use SMB, FTP or email on these devices |

The ranking order is Judgment, based on reach, cost and risk.

Changes from the first-pass ranking:

- Phone upload moves above email, because it is smaller and helps every user.
- WSD push stays at rank 4. `wsd.py` does not handle it today.
- WebDAV enters at rank 5.
- IPP Scan drops to near the bottom, because no shipping device was found.
- The embedded SMB server is "Never" for now (see its section).

## 1. Scan to SMB (network folder)

**What it is.** The scanner writes the file to a Windows-style shared folder. OpenScanHub then picks up the file.

**Reach.** Very high (Judgment, from many vendors that document it):

- Brother: "Scan to Network Folder" uses SMB on ports 139 and 445, with NTLM and Kerberos authentication. https://support.brother.com/g/s/id/smb/stn/stn-a-scan.html
- Brother has an SMBv3 client with SMB encryption (AES-CCM) and signing (AES-CMAC). The page names no models. https://www.brother.com.ph/en/solutions/smbv3scanning
- Canon: "Scan to File Server (SMB)". Canon says Samba 2.2.8a or later is supported. https://sg.canon/en/support/8200292000
- ScanSnap iX1600 and iX2500 save straight to a network folder without a computer. https://www.pfu.ricoh.com/imaging/downloads/manual/scansnap_help/en/pc/topic/ope_scan_nwfolder.html
- ScanSnap needs SMB "Version: 1.0 to 3.1.1" and NTLMv2. It lists "Samba 3.6 or later" as a supported host. https://www.pfu.ricoh.com/imaging/downloads/manual/scansnap_help/en/pc/topic/ope_basic_networkfolder.html

**SMB version risk.** Some older scanners speak only SMBv1. Windows no longer installs SMBv1 by default since Windows 10 version 1709. Microsoft says such devices need a firmware update. https://learn.microsoft.com/en-us/windows-server/storage/file-server/troubleshoot/smbv1-not-installed-by-default-in-windows

**How to build it.** Two parts. Neither embeds Samba, which is GPL.

1. Recipe, size S. The host OS or the NAS serves the share.
2. Point folder watch at the shared folder. Folder watch exists today, so this part is docs and a wizard step.
3. Remote share poll, size M. OpenScanHub logs in to a NAS share and polls it for new files.

For Docker, the share folder is a bind mount. Samba stays a separate program, so its license does not touch OpenScanHub.

For the remote poll, use smbprotocol. It is an MIT client for SMB 2.0.2 to 3.1.1, with SMB 3 encryption. https://pypi.org/project/smbprotocol/

**Spec.** Open. The MS-SMB2 page itself was not opened in this pass.

**Test without hardware.** Run Samba in a container. Copy files in with `smbclient` to act as the scanner (Judgment: standard tools, not tried in this pass).

**Recommendation.** Build in 0.4.0: the recipe first, then the remote poll.

## 2. Phone camera upload in the web app

**What it is.** A page in the web app where a phone takes a photo of a page. The browser uploads it, and OpenScanHub runs it through the normal output pipeline.

**Facts.** `<input type="file" accept="image/*" capture="environment">` asks the browser to use the rear camera. MDN says the attribute "works better on mobile devices". It also says the feature "is not Baseline", so a desktop gets a normal file picker. https://developer.mozilla.org/en-US/docs/Web/HTML/Reference/Attributes/capture

**Reach.** Anyone with a phone, with or without a scanner (Judgment).

**Libraries.** None new. The web server and Pillow are already there.

**Not verified.** Which phone browsers honor `capture` today. The MDN compatibility table did not come through. HEIC photos from iPhones may need conversion; this was not checked.

**Test without hardware.** Upload a JPEG to the endpoint with a test client. The camera path needs a real phone once.

**Cost.** S. Page cropping or perspective correction is a separate, larger task.

**Recommendation.** Build in 0.4.0.

## 3. Email in (IMAP)

**What it is.** The scanner emails the scan. OpenScanHub polls a mailbox over IMAP, saves the attachments, and marks the message as done.

**Reach.** High. Brother "Scan to E-mail Server" sends scans as attachments and needs an SMTP server set on the device. https://support.brother.com/g/s/id/smb/ste/ste-a-scan.html. Other vendors have the same kind of feature (Not verified in detail).

**Libraries.**

- `imaplib` is in the Python standard library. `IMAP4_SSL` uses port 993. https://docs.python.org/3/library/imaplib.html
- `IMAP4.idle()` (RFC 2177) was added in Python 3.14. OpenScanHub supports Python 3.10 and later, so poll by default. Use IDLE only where it exists.
- With the default SSL context, `imaplib` does not verify the certificate. Pass `ssl.create_default_context()`.
- Option: an embedded SMTP receiver with aiosmtpd (Apache-2.0, last release 1.4.6 in May 2024). It needs no mailbox. Its STARTTLS and AUTH support was not checked. https://pypi.org/project/aiosmtpd/

**Test without hardware.** A local IMAP server in a container, with messages added by a script (Judgment, not tried).

**Risk.** Big mail providers often need OAuth, not a password. Not verified for any provider in this pass.

**Cost.** M.

**Recommendation.** Build in 0.4.0: IMAP poll of a dedicated mailbox. Keep the SMTP receiver for later.

## 4. WSD ScanAvailableEvent (scan button push over WSD)

**What it is.** A client sends `wse:Subscribe` with a `ScanDestinations` element to the WSD Scan Service. A user then picks that destination on the scanner panel and starts a scan. The device sends a `ScanAvailableEvent` to the client's event sink, with `ClientContext` and `ScanIdentifier`. https://learn.microsoft.com/en-us/windows-hardware/drivers/image/scanavailableevent

The client then sends `ScanIdentifier` in a `CreateScanJobRequest`, so the device knows the right client asked. The value is unique for each event. https://learn.microsoft.com/en-us/windows-hardware/drivers/image/scanidentifier

**Does wsd.py do it today?** No. The module pulls only. It has no subscription, no event sink and no renewal.

**What a build needs.** An HTTP event sink in OpenScanHub, a Subscribe call per scanner, and renewal before `Expires`. The existing CreateScanJob path then adds `ScanIdentifier`.

**Reach.** Any scanner that already works with `wsd.py` and lists computers on its panel (Judgment). Which models list WSD destinations was not checked: Not verified.

**Spec.** Published by Microsoft as schema docs.

**Test without hardware.** No WSD scan simulator was found; Not verified that one exists. A unit test can replay the XML examples from the Microsoft page. The full path needs a real device.

**Cost.** M to L.

**Recommendation.** Later (0.5). It is the best "press the button on the scanner" path. It cannot be proven end to end without a device.

## 5. WebDAV receiver

**What it is.** OpenScanHub runs a small WebDAV server. The scanner uploads files to it with HTTP PUT.

**Reach.** Mostly office MFPs:

- Konica Minolta bizhub has "WebDAV Send" under Scan. https://manuals.konicaminolta.eu/bizhub-C3851FS-C3851-C3351-UD/EN/contents/id02-_102366045.html
- Its WebDAV client settings include proxy, user name, password, chunk transfer and certificate checks. https://manuals.konicaminolta.eu/ineo-6136-6120/EN/contents/UTL_103891087.html
- Canon: a forum thread about an imageRUNNER and SharePoint WebDAV showed in search results. The page was not opened: Not verified.
- Sharp, Ricoh, Kyocera and Brother: Not verified.

**Library.** WsgiDAV is MIT and serves a read-write filesystem share. https://github.com/mar10/wsgidav. Current version 4.4.0. https://pypi.org/project/WsgiDAV/

**Test without hardware.** Push files with any WebDAV client, for example `curl -T` (Judgment).

**Cost.** S to M.

**Recommendation.** Later. Few home scanners offer it. Revisit if users ask.

## 6. SANE network client (saned)

**What it is.** OpenScanHub talks to `saned` on another machine that has the scanner on USB. It uses the SANE network protocol to list devices, set options and pull the image.

**Protocol facts.** Source: https://sane-project.gitlab.io/standard/net.html

- 32-bit words, big-endian. Strings are Latin-1 with a trailing NUL.
- RPC codes 0 to 10: INIT, GET_DEVICES, OPEN, CLOSE, GET_OPTION_DESCRIPTORS, CONTROL_OPTION, GET_PARAMETERS, START, CANCEL, AUTHORIZE, EXIT.
- Image data goes on a second TCP connection, to a port that START returns.
- Data records are a 4-byte length plus data. `0xffffffff` ends the stream.
- Passwords go in plain text. An optional MD5 challenge exists.

**Port.** `saned` defaults to "sane-port (6566)". The data port range is set with `data_portrange`. Access control is in `saned.conf`. https://manpages.debian.org/testing/sane-utils/saned.8.en.html

**License.** The SANE API and network protocol are "considered to be in the public domain". So we can write our own client under MIT. https://gitlab.com/sane-project/backends/-/raw/master/LICENSE

Most backend libraries are GPL with a linking exception. The exact text, from `backend/dll.c`, says: "if you link a SANE library with other files to produce an executable, this does not by itself cause the resulting executable to be covered by the GNU General Public License." The LICENSE file warns that not all backends carry the exception. https://gitlab.com/sane-project/backends/-/raw/master/backend/dll.c

**Existing Python library.** python-sane wraps libsane through a C extension (`_sane.c`). It needs the SANE dev package. Its COPYING is an HPND (MIT-style) notice. https://github.com/python-pillow/Sane and https://raw.githubusercontent.com/python-pillow/Sane/main/COPYING

It cannot run on Windows or in a slim container without libsane (Judgment). No pure-Python saned client was found; Not verified that none exists.

**Test without hardware.** Run `saned` with the `test` backend. It "simulates scanning and setting options" with test pictures. https://manpages.debian.org/testing/libsane-common/sane-test.5.en.html

**Cost.** M to L.

**Recommendation.** Later. It is easy to test and has a clean license, but it serves few users.

## 7. Brother scan key push (brscan-skey)

**What it is.** Brother's closed Linux tool registers the computer on the scanner panel. It gets a network message when the user presses Scan, then pulls the scan.

**Spec.** No public Brother spec was found.

**Open projects.**

- brotherscankeyd registers entries with SNMP set requests (`snmpset`) and listens for UDP packets. It is GPL-3.0 and stopped in 2020. The successor is brotherscankeyd2. https://github.com/FrankAbelbeck/brotherscankeyd
- brotherscankeyd2 is GPL-3.0. Its README gives no OID, port or transfer details. https://github.com/FrankAbelbeck/brotherscankeyd2
- brother-scan publishes UDP port 54925 and depends on Brother's proprietary brscan4 package. Its README does not name the license. https://github.com/crazyduck/brother-scan

**License flag.** Do not copy code from the GPL-3.0 projects into MIT OpenScanHub. A clean build needs our own packet captures.

**Not verified.** The SNMP OIDs, the UDP payload format, and how the image is fetched after the event. The sample scripts appear to use SANE for it.

**Test without hardware.** Not possible without captures. None were found.

**Cost.** L.

**Recommendation.** Later, only with demand. Most Brother network models can also do SMB, FTP or email, and some do eSCL or WSD (Judgment).

## 8. TWAIN and WIA on Windows

**What it is.** OpenScanHub drives a local (USB) scanner through the Windows driver stacks.

**Libraries and licenses.**

- The TWAIN Data Source Manager is open source under the LGPL. https://github.com/twain/twain-dsm
- pytwain 2.3.0 has the GPLv2 classifier on PyPI. Bundling it would put the bundle under GPL terms (Judgment). https://pypi.org/pypi/pytwain/json
- pywin32 312 is under the PSF license, which is permissive. A WIA client through COM would use it. https://pypi.org/pypi/pywin32/json
- No working WIA scan sample was checked: Not verified.

**Test without hardware.** Not verified. No virtual TWAIN source was checked.

**Cost.** L.

**Recommendation.** Later, only with demand. Network scanners are the core use.

## 9. Embedded pure-Python SMB server (impacket)

**What it is.** OpenScanHub runs its own SMB server, so the scanner writes to it directly with no Samba.

**License.** "A slightly modified Apache Software License (Version 1.1)". The changes only swap in the names Impacket and Fortra. Source: https://github.com/fortra/impacket/blob/master/LICENSE

- Condition 3 needs a credit line in the docs or the software.
- Conditions 4 and 5 limit use of the names.
- Some files carry other permissive licenses (MIT, BSD-style).
- The terms allow MIT distribution, with the credit line added (Judgment).

**SMB versions (checked in the source).**

- PyPI's current release is 0.13.1. https://pypi.org/pypi/impacket/json
- In 0.13.1, the server answers SMB2 negotiation with `SMB2_DIALECT_002` (SMB 2.0.2) only. https://raw.githubusercontent.com/fortra/impacket/impacket_0_13_1/impacket/smbserver.py
- The unreleased `master` branch has `_SUPPORTED_DIALECTS = (SMB2_DIALECT_311, SMB2_DIALECT_21, SMB2_DIALECT_002)`. It adds code for encryption and pre-auth hashing. SMB 3.0 and 3.0.2 are not in the list. https://raw.githubusercontent.com/fortra/impacket/master/impacket/smbserver.py
- Search results say the example script turns SMB2 on only with `-smb2support`, marked experimental. The page was not opened: Not verified.

**Dependencies.** impacket requires pyOpenSSL, ldap3, ldapdomaindump, flask, pycryptodomex, pyasn1 and more. https://pypi.org/pypi/impacket/json

**Would SMB2/3 scanners work?** Not verified. A scanner that demands SMB 3 encryption would fail against 0.13.1. The Brother SMBv3 client offers encryption, but it is not known whether it demands it. A scanner that accepts SMB 2.0.2 might work. No device was tested.

**Other risk.** impacket is best known as a security testing toolkit. Antivirus tools may flag it: Not verified.

**Recommendation.** Never in its current state. Revisit if a release ships SMB 3.x and a real scanner test passes. Rank 1, a share the OS serves, gives the same result with less risk.

## 10. IPP Scan (PWG 5100.17)

**What it is.** The abstract says it "implements a protocol binding of the PWG Semantic Model Scan service over IPP." It has `Create-Job` and `Get-Next-Document-Data`, plus an optional Push Scan with `destination-uris`. https://ftp.pwg.org/pub/pwg/candidates/cs-ippscan10-20140918-5100.17.pdf

**Status.** Candidate Standard since 2014. https://www.pwg.org/archives/pwg-announce/2014/003631.html

Search results quote a 2017 mailing list post: no shipping device was known, and CUPS would not add IPP Scan to ippserver. Search results also quote PWG 2021 notes that deferred IPP Scan work. Neither page was opened: Not verified.

**Libraries.** pyipp is an MIT async IPP client for printer status, with no scan support. https://github.com/ctalkington/python-ipp

ippsample, the PWG sample code, is Apache 2.0. Its README lists no scan server. https://github.com/istopwg/ippsample

**Test without hardware.** No simulator found.

**Recommendation.** Never, unless devices ship it. eSCL already covers driverless scanning.

## 11. Vendor push apps (Epson, Canon, ScanSnap)

- Epson "Scan to Computer" needs Document Capture Pro on the computer. https://epson.com/faq/SPT_B11B226201~faq-0000643-shared
- Older Epson "Push-Scan" needs Epson Event Manager running. https://epson.co.uk/faq/KA-01556/contents?loc=en-us
- Both Epson tools are closed apps. No public protocol was found.
- ScanSnap can save straight to an SMB folder (see section 1), so OpenScanHub covers it through SMB.
- Canon scan to computer through MF Scan Utility: Not verified. Canon MFPs do SMB (see section 1).

**Recommendation.** Never. Point users to the SMB, FTP or email feature on the same device.

## Licensing summary

| Item | License | OK to ship inside MIT OpenScanHub? |
|---|---|---|
| SANE network protocol spec | Public domain | Yes. Write our own client. |
| SANE backends (libsane) | GPL with linking exception, not on every backend | Linking is allowed by the exception. Do not copy code. Avoid bundling. |
| python-sane | HPND (MIT-style) | Yes, but it needs libsane on the host. |
| smbprotocol | MIT | Yes |
| Samba | GPL | Do not embed. Run it as a separate program. |
| impacket | Modified Apache 1.1, credit clause | Yes with a credit line. Not recommended (see section 9). |
| WsgiDAV | MIT | Yes |
| aiosmtpd | Apache-2.0 | Yes. Keep its license and notice terms. |
| imaplib | Python standard library | Yes |
| pyipp | MIT | Yes (no scan support) |
| ippsample | Apache 2.0 | Yes (no scan support) |
| pytwain | GPLv2 | No, not inside the MIT bundle |
| TWAIN DSM | LGPL | Use the system copy. Do not bundle a modified copy. |
| pywin32 | PSF | Yes |
| brotherscankeyd, brotherscankeyd2 | GPL-3.0 | No. Do not copy code. |

The "OK to ship" column is Judgment, not legal advice.

## Not verified

- Which phone browsers honor the `capture` attribute today.
- Which scanner models offer WSD scan destinations on the panel.
- Any WSD scan simulator, and any virtual TWAIN source.
- Brother scan key protocol details: SNMP OIDs, UDP payload, image transfer.
- Whether any scanner works with the impacket SMB server, and antivirus behavior.
- aiosmtpd STARTTLS and AUTH support.
- WebDAV support on Canon, Sharp, Ricoh, Kyocera and Brother.
- Current shipping devices with IPP Scan.
- Mail provider OAuth needs for IMAP.

## Revision history

| Date | Change |
|---|---|
| 2026-11-06 | First version. |
| 2026-11-06 | Added the SMB share poll and email in (IMAP) to the supported table. |
