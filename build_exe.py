"""PyInstaller build script for the OpenScanHub Windows EXE.

Install the pinned dependencies first so the build is reproducible:
    pip install --require-hashes -r requirements.lock
    pip install --no-deps -e .
    python build_exe.py
"""

import os
import sys
from pathlib import Path


def build():
    try:
        import PyInstaller.__main__
    except ImportError:
        print("ERROR: PyInstaller not installed. Run: pip install --require-hashes -r requirements.lock")
        sys.exit(1)

    root = Path(__file__).parent
    main_script = root / "openscan" / "__main__.py"
    static_dir = root / "openscan" / "web" / "static"

    args = [
        str(main_script),
        "--name", "OpenScanHub",
        "--onefile",
        "--windowed",
        "--noconfirm",
        "--add-data", f"{static_dir}{os.pathsep}openscan/web/static",
        "--add-data", f"{root / 'LICENSE'}{os.pathsep}.",
        "--add-data", f"{root / 'THIRD_PARTY_NOTICES.md'}{os.pathsep}.",
        # Every openscan module, including ones imported lazily.
        "--collect-submodules", "openscan",
        "--hidden-import", "pyftpdlib.authorizers",
        "--hidden-import", "pyftpdlib.handlers",
        "--hidden-import", "pyftpdlib.servers",
        "--hidden-import", "zeroconf",
        "--hidden-import", "pystray",
        "--hidden-import", "PIL",
        "--hidden-import", "pypdfium2",
        "--collect-binaries", "pypdfium2_raw",
        "--distpath", str(root / "dist"),
        "--workpath", str(root / "build"),
        "--specpath", str(root / "build"),
    ]

    print("Building OpenScanHub.exe...")
    PyInstaller.__main__.run(args)
    print(f"\nBuild complete: {root / 'dist' / 'OpenScanHub.exe'}")


if __name__ == "__main__":
    build()
