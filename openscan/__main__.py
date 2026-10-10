"""Allow running as `python -m openscan` or as the PyInstaller entry point."""
if __package__:
    from .main import main
else:  # PyInstaller runs this file as a script
    from openscan.main import main

main()
