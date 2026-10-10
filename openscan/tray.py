"""System tray icon for Windows (via pystray)."""

import logging
import threading
import webbrowser
from typing import Callable, Optional

logger = logging.getLogger(__name__)


def _url(config, page: str = "") -> str:
    return f"http://localhost:{config.web.port}/{page}"


def create_tray(config, on_quit: Optional[Callable[[], None]] = None) -> bool:
    """Create and run the tray icon. Blocks until Quit. Returns False if unavailable."""
    try:
        import pystray
    except ImportError:
        logger.warning("pystray is not installed; no tray icon")
        return False

    from . import autostart

    quit_callback = on_quit

    def notify(icon, message: str) -> None:
        try:
            icon.notify(message, "OpenScanHub")
        except Exception:
            logger.info(message)

    def scan(icon, source_name: str) -> None:
        from .scanner.base import ScanSource
        from .web.dashboard_api import BUSY_MESSAGE, start_scan_async
        from .web.server import BadRequest

        source = ScanSource.ADF if source_name == "adf" else ScanSource.FLATBED
        try:
            if start_scan_async(source):
                notify(icon, "Scanning. The file will appear in your scans folder.")
            else:
                notify(icon, BUSY_MESSAGE)
        except BadRequest as e:
            notify(icon, str(e))
        except Exception as e:
            logger.error("Tray scan failed: %s", e)
            notify(icon, f"Scan failed: {e}")

    def on_open_dashboard(icon, item):
        webbrowser.open(_url(config))

    def on_scan_adf(icon, item):
        threading.Thread(target=scan, args=(icon, "adf"), daemon=True).start()

    def on_scan_flatbed(icon, item):
        threading.Thread(target=scan, args=(icon, "flatbed"), daemon=True).start()

    def on_show_token(icon, item):
        threading.Thread(target=show_token, args=(config,), daemon=True).start()

    def on_open_logs(icon, item):
        from .logging_setup import get_log_dir
        from .web.settings_api import open_folder

        log_dir = get_log_dir()
        if log_dir is not None:
            open_folder(log_dir)

    def on_toggle_autostart(icon, item):
        if autostart.is_enabled():
            autostart.disable()
        else:
            autostart.enable()

    def on_quit_item(icon, item):
        icon.stop()
        if quit_callback:
            quit_callback()

    menu = pystray.Menu(
        pystray.MenuItem("Open dashboard", on_open_dashboard, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Scan from feeder", on_scan_adf),
        pystray.MenuItem("Scan from flatbed", on_scan_flatbed),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Show access token", on_show_token),
        pystray.MenuItem("Open log folder", on_open_logs),
        pystray.MenuItem("Start with Windows", on_toggle_autostart,
                         checked=lambda item: autostart.is_enabled(),
                         visible=autostart.is_supported()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit", on_quit_item),
    )

    icon = pystray.Icon("OpenScanHub", _create_icon_image(), "OpenScanHub", menu)
    icon.run()
    return True


def show_token(config) -> None:
    """Copy the token to the clipboard and show it; fall back to the Settings page."""
    from .web.server import get_config

    try:
        token = get_config().web.api_token
    except AssertionError:
        token = config.web.api_token
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        root.clipboard_clear()
        root.clipboard_append(token)
        root.update()
        messagebox.showinfo(
            "OpenScanHub access token",
            f"{token}\n\nThis token is copied to your clipboard. Enter it when another "
            "device asks you to sign in to OpenScanHub.",
        )
        root.destroy()
    except Exception as e:
        logger.debug("No message box (%s); opening Settings instead", e)
        webbrowser.open(_url(config, "settings#access"))


def _create_icon_image():
    """Create a simple icon image without needing an external file."""
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse([2, 2, size - 2, size - 2], fill=(37, 99, 235))
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except (OSError, IOError):
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), "S", font=font)
    x = (size - (bbox[2] - bbox[0])) // 2
    y = (size - (bbox[3] - bbox[1])) // 2 - 2
    draw.text((x, y), "S", fill="white", font=font)
    return img
