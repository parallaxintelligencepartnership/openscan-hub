"""Allow running as `python -m openscan` or as PyInstaller entry point."""
try:
    from .main import main
except ImportError:
    from openscan.main import main

main()
