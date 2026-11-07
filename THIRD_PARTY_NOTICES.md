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
| Pillow | MIT-CMU (HPND) | Turning scanned images into PDF pages; deskew, crop, gray and black-and-white pages | https://github.com/python-pillow/Pillow |
| LittleCMS (inside Pillow) | MIT | The sRGB color profile in PDF/A files | https://github.com/mm2/Little-CMS |
| zxing-cpp | Apache-2.0 | Reading barcodes and QR codes on separator sheets | https://github.com/zxing-cpp/zxing-cpp |
| Zint (inside zxing-cpp) | BSD-3-Clause | Barcode writing in zxing-cpp (OpenScanHub only reads) | https://github.com/zint/zint |

## Separate programs (not bundled in the EXE)

| Program | License | Used for | Source |
|---|---|---|---|
| Tesseract OCR | Apache-2.0 | Text recognition and page orientation. The Docker image installs it from Debian with English, German, French and Spanish language data (Apache-2.0) and the libraries it needs, among them Leptonica (BSD-2-Clause style); each Debian package keeps its copyright file in `/usr/share/doc`. On Windows you install it yourself; OpenScanHub looks for `tesseract` on PATH and in the standard `Tesseract-OCR` install folders. | https://github.com/tesseract-ocr/tesseract |

OpenScanHub runs Tesseract as a separate program and does not link it.
OpenScanHub does not use Ghostscript (AGPL); PDF/A output is made with
pypdf and Pillow.

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
