"""OpenScanHub entry point."""

import argparse
import logging
import platform
import sys
import threading
import webbrowser

from . import __app_name__, __version__
from .config import load_config, save_config, get_config_path

logger = logging.getLogger(__name__)


def setup_logging(level: str = "INFO") -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="openscan",
        description=f"{__app_name__} v{__version__} - Universal network scanner bridge",
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Web UI port (default: 8020)",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Don't auto-open browser on first run",
    )
    parser.add_argument(
        "--log-level", default=None,
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="Logging level",
    )
    parser.add_argument(
        "--version", action="version",
        version=f"{__app_name__} {__version__}",
    )

    args = parser.parse_args()

    config = load_config()

    # CLI args override config
    if args.port:
        config.web_port = args.port
    if args.log_level:
        config.log_level = args.log_level

    setup_logging(config.log_level)

    logger.info("%s v%s starting", __app_name__, __version__)
    logger.info("Config: %s", get_config_path())
    logger.info("Platform: %s", platform.system())

    # Start ADF monitor if configured
    if config.wizard_completed and config.monitor.enabled:
        from .monitor import start_monitor
        start_monitor(config)

    # Start folder watcher if configured
    if config.wizard_completed and config.folder_watch.enabled:
        from .scanner.folder_watch import start_folder_watcher
        start_folder_watcher(config)

    # Start web server
    from .web.server import run_server

    # Auto-open browser on first run (not in Docker)
    if not args.no_browser and _should_open_browser():
        url = f"http://localhost:{config.web_port}"
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        logger.info("Opening browser to %s", url)

    # Start system tray on Windows if running as frozen EXE
    if _is_frozen() and platform.system() == "Windows":
        # Run web server in a background thread, tray in main thread
        server_thread = threading.Thread(
            target=run_server,
            args=(config,),
            daemon=True,
        )
        server_thread.start()
        _start_tray(config)
    else:
        # Run web server in main thread
        run_server(config)


def _should_open_browser() -> bool:
    """Determine if we should auto-open a browser."""
    import os
    # Don't open in Docker
    if os.path.exists("/.dockerenv") or os.environ.get("OPENSCAN_DOCKER"):
        return False
    # Don't open in SSH sessions
    if os.environ.get("SSH_CONNECTION"):
        return False
    return True


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _start_tray(config):
    """Start the system tray icon (Windows only)."""
    try:
        from .tray import create_tray
        create_tray(config)
    except ImportError:
        logger.warning("pystray not available, running without system tray")


if __name__ == "__main__":
    main()
