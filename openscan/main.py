"""OpenScanHub entry point."""

import argparse
import errno
import logging
import os
import platform
import signal
import sys
import threading
import webbrowser
from typing import Optional

from . import __app_name__, __version__
from .config import _is_docker, get_config_dir, get_config_path, load_config, set_override
from .logging_setup import get_log_dir, setup_logging

logger = logging.getLogger(__name__)

_ADDR_IN_USE = {errno.EADDRINUSE, 10048}  # 10048 = WSAEADDRINUSE


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="openscan",
        description=f"{__app_name__} v{__version__} - network scanner bridge",
    )
    parser.add_argument("--port", type=int, default=None, help="Web UI port (default: 8020)")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser on start")
    parser.add_argument("--log-level", default=None, choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="Logging level")
    parser.add_argument("--show-token", action="store_true",
                        help="Print the access token and exit")
    parser.add_argument("--version", action="version", version=f"{__app_name__} {__version__}")
    return parser


def browser_url(port: int) -> str:
    return f"http://localhost:{port}/"


def show_error_box(title: str, message: str) -> None:
    """Message box on Windows when tkinter is available; the log has it either way."""
    if platform.system() != "Windows":
        return
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(title, message)
        root.destroy()
    except Exception as e:
        logger.debug("No message box available: %s", e)


def run(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # Logging first, so even a broken config leaves a trace in the log file.
    setup_logging(level=args.log_level or "INFO")

    config = load_config()
    if args.port:
        set_override(config, "web", "port", args.port)
    if args.log_level:
        set_override(config, "", "log_level", args.log_level)
    setup_logging(config)

    if args.show_token:
        print(config.web.api_token)
        return 0

    logger.info("%s v%s starting on %s", __app_name__, __version__, platform.system())
    logger.info("Config file: %s", get_config_path())
    log_dir = get_log_dir()
    logger.info("Log folder: %s", log_dir if log_dir else "(not writable, console only)")
    logger.info("Web UI: bind %s, port %d", config.web.bind, config.web.port)
    # Never log the token itself: logs get shared in bug reports.
    logger.info("Access token for other devices: see Settings, or run "
                "'openscan --show-token' (in Docker: 'python -m openscan --show-token')")

    lock = None
    if not _is_docker():
        from .singleinstance import InstanceLock

        lock = InstanceLock(get_config_dir())
        other = lock.acquire(config.web.port, config.web.bind)
        if other is not None:
            url = browser_url(int(other.get("port") or config.web.port))
            logger.info("OpenScanHub is already running (pid %s); opening %s", other.get("pid"), url)
            if not args.no_browser:
                webbrowser.open(url)
            return 0

    from .web.server import create_server

    try:
        server = create_server(config)
    except OSError as e:
        if lock:
            lock.release()
        if e.errno in _ADDR_IN_USE or getattr(e, "winerror", None) == 10048:
            message = (f"OpenScanHub could not start because port {config.web.port} is already in use "
                       "by another program. Close that program, or start OpenScanHub with "
                       "--port 8021 (or set OPENSCAN_PORT).")
        else:
            message = f"OpenScanHub could not start its web page on port {config.web.port}: {e}"
        logger.error(message)
        show_error_box("OpenScanHub", message)
        return 1

    from . import services

    port = server.server_address[1]
    services.set_running_web(config.web.bind, port)
    report = services.start_all(config)
    for problem in report["errors"]:
        logger.warning(problem)

    if not args.no_browser and _should_open_browser():
        url = browser_url(port)
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        logger.info("Opening browser to %s", url)

    _stop_on_sigterm()
    try:
        if _is_frozen() and platform.system() == "Windows":
            threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
            _start_tray(config, server)
        else:
            server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Stopping")
    finally:
        services.stop_all()
        server.server_close()
        if lock:
            lock.release()
    return 0


def main() -> None:
    raise SystemExit(run())


def _stop_on_sigterm() -> None:
    """Treat SIGTERM (docker stop, kill) like Ctrl+C so services stop cleanly."""
    def handler(signum, frame):
        raise KeyboardInterrupt

    if threading.current_thread() is threading.main_thread() and hasattr(signal, "SIGTERM"):
        try:
            signal.signal(signal.SIGTERM, handler)
        except (ValueError, OSError):
            pass


def _should_open_browser() -> bool:
    if _is_docker():
        return False
    if os.environ.get("SSH_CONNECTION"):
        return False
    return True


def _is_frozen() -> bool:
    return getattr(sys, "frozen", False)


def _start_tray(config, server) -> None:
    """Run the tray icon on the main thread until Quit; without pystray, just serve."""
    stop = threading.Event()

    def on_quit():
        server.shutdown()
        stop.set()

    try:
        from .tray import create_tray
    except ImportError:
        create_tray = None
    if create_tray is None or not create_tray(config, on_quit=on_quit):
        logger.warning("No tray icon available; OpenScanHub keeps running in the background")
        stop.wait()


if __name__ == "__main__":
    main()
