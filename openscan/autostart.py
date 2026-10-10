"""Start OpenScanHub when the user signs in to Windows (HKCU Run key).

On other platforms every function is a logged no-op.
"""

import logging
import sys
from typing import Optional

logger = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "OpenScanHub"


def _winreg():
    """Return the winreg module on Windows, else None. Tests replace this."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None
    return winreg


def command() -> str:
    """The command line Windows runs at sign-in."""
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --no-browser'
    exe = sys.executable
    if exe.lower().endswith("python.exe"):
        exe = exe[: -len("python.exe")] + "pythonw.exe"
    return f'"{exe}" -m openscan --no-browser'


def is_supported() -> bool:
    return _winreg() is not None


def is_enabled() -> bool:
    reg = _winreg()
    if reg is None:
        return False
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_READ) as key:
            value, _ = reg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def enable(cmd: Optional[str] = None) -> bool:
    reg = _winreg()
    if reg is None:
        logger.info("Start with Windows is only available on Windows")
        return False
    try:
        with reg.CreateKeyEx(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.SetValueEx(key, VALUE_NAME, 0, reg.REG_SZ, cmd or command())
        logger.info("Start with Windows turned on")
        return True
    except OSError as e:
        logger.error("Could not turn on Start with Windows: %s", e)
        return False


def disable() -> bool:
    reg = _winreg()
    if reg is None:
        logger.info("Start with Windows is only available on Windows")
        return False
    try:
        with reg.OpenKey(reg.HKEY_CURRENT_USER, RUN_KEY, 0, reg.KEY_SET_VALUE) as key:
            reg.DeleteValue(key, VALUE_NAME)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.error("Could not turn off Start with Windows: %s", e)
        return False
    logger.info("Start with Windows turned off")
    return True
