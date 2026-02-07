"""PyInstaller build script for OpenScanHub Windows EXE."""

import os
import sys
from pathlib import Path

def build():
    try:
        import PyInstaller.__main__
    except ImportError:
        print("ERROR: PyInstaller not installed. Run: pip install pyinstaller")
        sys.exit(1)

    root = Path(__file__).parent
    main_script = root / "openscan" / "__main__.py"
    static_dir = root / "openscan" / "web" / "static"

    args = [
        str(main_script),
        "--name", "OpenScanHub",
        "--onefile",
        "--windowed",
        "--add-data", f"{static_dir}{os.pathsep}openscan/web/static",
        "--hidden-import", "openscan",
        "--hidden-import", "openscan.main",
        "--hidden-import", "openscan.config",
        "--hidden-import", "openscan.discovery",
        "--hidden-import", "openscan.output",
        "--hidden-import", "openscan.monitor",
        "--hidden-import", "openscan.tray",
        "--hidden-import", "openscan.scanner.escl",
        "--hidden-import", "openscan.scanner.wsd",
        "--hidden-import", "openscan.scanner.folder_watch",
        "--hidden-import", "openscan.scanner.ftp_receive",
        "--hidden-import", "pyftpdlib",
        "--hidden-import", "pyftpdlib.authorizers",
        "--hidden-import", "pyftpdlib.handlers",
        "--hidden-import", "pyftpdlib.servers",
        "--hidden-import", "openscan.web.server",
        "--hidden-import", "openscan.web.wizard_api",
        "--hidden-import", "openscan.web.dashboard_api",
        "--hidden-import", "zeroconf",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--distpath", str(root / "dist"),
        "--workpath", str(root / "build"),
        "--specpath", str(root / "build"),
    ]

    print("Building OpenScanHub.exe...")
    PyInstaller.__main__.run(args)
    print(f"\nBuild complete: {root / 'dist' / 'OpenScanHub.exe'}")


if __name__ == "__main__":
    build()
