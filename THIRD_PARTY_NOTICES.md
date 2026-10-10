# Third-party notices

OpenScanHub itself is released under the MIT License (see `LICENSE`).
It uses the open-source packages below. The Windows EXE and the Docker image
bundle them; each keeps its own license. Exact versions are pinned in
`requirements.lock`.

## Runtime dependencies (every install)

| Package | License | Used for | Source |
|---|---|---|---|
| zeroconf | LGPL-2.1-or-later | Finding scanners on the network (mDNS) | https://github.com/python-zeroconf/python-zeroconf |
| ifaddr (needed by zeroconf) | MIT | Listing network interfaces | https://github.com/ifaddr/ifaddr |
| pyftpdlib | MIT | The built-in FTP receiver | https://github.com/giampaolo/pyftpdlib |
| pyasyncore, pyasynchat (needed by pyftpdlib on Python 3.12+) | PSF-2.0 | Networking for pyftpdlib | https://github.com/simonrob/pyasyncore |
| pypdf | BSD-3-Clause | Joining pages into one PDF | https://github.com/py-pdf/pypdf |
| pypdfium2 | Apache-2.0 OR BSD-3-Clause | Page thumbnails | https://github.com/pypdfium2-team/pypdfium2 |
| PDFium (binary inside pypdfium2) | Apache-2.0 OR BSD-3-Clause, plus the notices of its bundled libraries (FreeType, libjpeg-turbo, LittleCMS, libpng, OpenJPEG, zlib, ICU and others) listed in pypdfium2's `LicenseRef-PdfiumThirdParty.txt` | PDF rendering | https://pdfium.googlesource.com/pdfium/ |
| Pillow | MIT-CMU (HPND) | Turning scanned images into PDF pages | https://github.com/python-pillow/Pillow |

## Windows EXE only

| Package | License | Used for | Source |
|---|---|---|---|
| pystray | LGPL-3.0 | The tray icon | https://github.com/moses-palmer/pystray |
| six (needed by pystray) | MIT | Compatibility helpers | https://github.com/benjaminp/six |
| PyInstaller bootloader | GPL-2.0-or-later with the PyInstaller bootloader exception, which allows distributing the EXE under any license | Starting the bundled app | https://github.com/pyinstaller/pyinstaller |
| CPython runtime | PSF-2.0 | Python itself | https://www.python.org/ |

On Linux, pystray also uses python-xlib (LGPL-2.1); on macOS it uses PyObjC
(MIT). Neither is part of the Windows EXE or the Docker image's runtime path.

## About the LGPL components

zeroconf and pystray are LGPL. OpenScanHub uses them as unmodified libraries.
Their source is available at the links above, and you may replace them with
your own build: in a pip or Docker install they are ordinary Python packages;
in the EXE they are Python modules that PyInstaller extracts at start-up.

## Build and test tools (not shipped)

PyInstaller (GPL-2.0-or-later, build only), pyinstaller-hooks-contrib
(GPL-2.0 or Apache-2.0), altgraph (MIT), pefile (MIT), pytest (MIT),
pluggy (MIT), iniconfig (MIT), Pygments (BSD-2-Clause), packaging
(Apache-2.0 OR BSD-2-Clause), setuptools (MIT).

Earlier versions (0.2.0 and before) bundled PyMuPDF, which is AGPL-3.0.
Version 0.3.0 removed it.
