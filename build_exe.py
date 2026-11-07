"""PyInstaller build script for the OpenScanHub Windows EXE.

Install the pinned dependencies first so the build is reproducible:
    pip install --require-hashes -r requirements.lock
    pip install --no-deps -e .
    python build_exe.py                      # OpenScanHub.exe (Homelab)
    python build_exe.py --edition office    # OpenScanHub-Office.exe

An Office build works on a temporary copy of the source tree. The working
tree is never changed. See docs/editions.md.
"""

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path

EDITIONS = ("homelab", "office")
# Left out of the Office tree: the code must not be in the EXE.
OFFICE_REMOVE = ("openscan/integrations", "openscan/destinations/paperless.py")
COPY_ITEMS = ("openscan", "LICENSE", "THIRD_PARTY_NOTICES.md")


def exe_name(edition: str) -> str:
    return "OpenScanHub-Office" if edition == "office" else "OpenScanHub"


def make_edition_tree(source_root: Path, edition: str, dest: Path) -> Path:
    """Copy the source to `dest` and apply `edition` there. Returns `dest`."""
    if edition not in EDITIONS:
        raise ValueError(f"edition must be one of {', '.join(EDITIONS)}")
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for item in COPY_ITEMS:
        src = Path(source_root) / item
        if src.is_dir():
            shutil.copytree(src, dest / item, ignore=ignore)
        elif src.is_file():
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest / item)
    sys.path.insert(0, str(source_root))
    try:
        from openscan.edition import set_edition
    finally:
        sys.path.pop(0)
    set_edition(edition, dest / "openscan" / "_edition.py")
    if edition == "office":
        for rel in OFFICE_REMOVE:
            target = dest / rel
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
    return dest


def build(edition: str = "homelab"):
    try:
        import PyInstaller.__main__
    except ImportError:
        print("ERROR: PyInstaller not installed. Run: pip install --require-hashes -r requirements.lock")
        sys.exit(1)

    out_root = Path(__file__).parent
    with tempfile.TemporaryDirectory(prefix="openscan-build-") as tmp:
        root = make_edition_tree(out_root, edition, Path(tmp) / "src")
        _run_pyinstaller(PyInstaller.__main__, root, out_root, edition)


def _run_pyinstaller(pyi, root: Path, out_root: Path, edition: str):
    # The temp tree must win over any installed copy of openscan.
    sys.path.insert(0, str(root))
    main_script = root / "openscan" / "__main__.py"
    static_dir = root / "openscan" / "web" / "static"

    # Belt and braces: an editable install can resolve these from the real
    # source tree, so name them as excluded too.
    excludes = []
    if edition == "office":
        for rel in OFFICE_REMOVE:
            excludes += ["--exclude-module", rel.removesuffix(".py").replace("/", ".")]

    args = [
        str(main_script),
        "--name", exe_name(edition),
        "--paths", str(root),
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
        "--hidden-import", "zxingcpp",
        "--hidden-import", "PIL.ImageCms",
        "--collect-binaries", "pypdfium2_raw",
        *excludes,
        "--distpath", str(out_root / "dist"),
        "--workpath", str(out_root / "build" / edition),
        "--specpath", str(out_root / "build" / edition),
    ]

    print(f"Building {exe_name(edition)}.exe...")
    pyi.run(args)
    print(f"\nBuild complete: {out_root / 'dist' / (exe_name(edition) + '.exe')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Build the OpenScanHub Windows EXE.")
    parser.add_argument("--edition", choices=EDITIONS, default="homelab")
    build(parser.parse_args().edition)
