"""System tray icon for Windows (via pystray)."""

import logging
import threading
import webbrowser

logger = logging.getLogger(__name__)


def create_tray(config):
    """Create and run the system tray icon. Blocks until quit."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        logger.warning("pystray/Pillow not installed - no system tray icon")
        return

    # Generate a simple icon (blue circle with white 'S')
    icon_image = _create_icon_image()

    def on_open_dashboard(icon, item):
        webbrowser.open(f"http://localhost:{config.web_port}")

    def on_scan_adf(icon, item):
        _trigger_scan("adf", config.web_port)

    def on_scan_flatbed(icon, item):
        _trigger_scan("flatbed", config.web_port)

    def on_settings(icon, item):
        webbrowser.open(f"http://localhost:{config.web_port}/wizard")

    def on_quit(icon, item):
        icon.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open Dashboard", on_open_dashboard, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Scan (Feeder)", on_scan_adf),
        pystray.MenuItem("Scan (Flatbed)", on_scan_flatbed),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Settings", on_settings),
        pystray.MenuItem("Quit", on_quit),
    )

    icon = pystray.Icon("OpenScanHub", icon_image, "OpenScanHub", menu)
    icon.run()


def _create_icon_image():
    """Create a simple icon image without needing an external file."""
    from PIL import Image, ImageDraw, ImageFont

    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # Blue circle background
    draw.ellipse([2, 2, size - 2, size - 2], fill=(37, 99, 235))

    # White 'S' letter
    try:
        font = ImageFont.truetype("arial.ttf", 36)
    except (OSError, IOError):
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), "S", font=font)
    text_w = bbox[2] - bbox[0]
    text_h = bbox[3] - bbox[1]
    x = (size - text_w) // 2
    y = (size - text_h) // 2 - 2
    draw.text((x, y), "S", fill="white", font=font)

    return img


def _trigger_scan(scan_type: str, port: int) -> None:
    """Trigger a scan via the local API."""
    from urllib.request import urlopen
    try:
        urlopen(f"http://localhost:{port}/api/scan/{scan_type}", timeout=60)
    except Exception as e:
        logger.error("Tray scan trigger failed: %s", e)
