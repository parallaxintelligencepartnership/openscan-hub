"""SMB share poll - pick up scans a scanner saves to a NAS share.

The scanner's "scan to network folder" writes a file to a share that a NAS
serves. OpenScanHub logs in to that share with smbprotocol (SMB 2 and 3),
polls one folder, and routes each new file through the standard pipeline.

A file is delivered once its size and time stay the same across two polls.
After delivery it moves to a "processed" subfolder or is deleted. A state
file in the config folder records every handled file (name, size, time),
so a file is never delivered twice, even after a restart or a failed move.
"""

import json
import logging
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional

from ..config import AppConfig, get_config_dir
from .. import intake
from .ftp_receive import sanitize_filename

logger = logging.getLogger(__name__)

SCAN_EXTENSIONS = {".pdf", ".jpg", ".jpeg", ".png", ".tiff", ".tif"}
PROCESSED_DIR = "processed"
STATE_FILE = "smb_poll_state.json"

_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_lock = threading.Lock()
_error: Optional[str] = None
_last_error: Optional[str] = None


def smb_poll_running() -> bool:
    return bool(_thread and _thread.is_alive())


def smb_poll_error() -> Optional[str]:
    """Why the poller refused to start (None if fine)."""
    return _error


def smb_poll_last_error() -> Optional[str]:
    """The problem of the last poll (None if the last poll worked)."""
    return _last_error


def check_settings(config: AppConfig) -> Optional[str]:
    c = config.smb_poll
    if not c.server.strip():
        return "Enter the server name or address of the share."
    if not c.share.strip():
        return "Enter the share name."
    if c.after_delivery not in ("move", "delete"):
        return "Choose what to do with a file after delivery."
    return None


def _clean_path(path: str) -> str:
    return "\\".join(p for p in path.replace("/", "\\").split("\\") if p)


def share_root(config: AppConfig) -> str:
    c = config.smb_poll
    return "\\\\" + c.server.strip() + "\\" + c.share.strip().strip("\\/")


def _kw(config: AppConfig) -> dict:
    """Connection options for every smbclient call (the port picks the session)."""
    return {"port": config.smb_poll.port or 445}


def folder_unc(config: AppConfig) -> str:
    sub = _clean_path(config.smb_poll.path)
    root = share_root(config)
    return root + ("\\" + sub if sub else "")


def _login(config: AppConfig) -> None:
    import smbclient
    c = config.smb_poll
    user = c.username
    if c.domain and user and "\\" not in user and "@" not in user:
        user = f"{c.domain}\\{user}"
    kwargs = {"port": c.port or 445}
    if user:
        kwargs.update(username=user, password=c.password)
    smbclient.register_session(c.server.strip(), **kwargs)


# --- state ------------------------------------------------------------------

def _state_path() -> Path:
    return get_config_dir() / STATE_FILE


def _state_key(config: AppConfig, name: str) -> str:
    c = config.smb_poll
    return f"{c.server.strip().lower()}|{c.share.strip().lower()}|{_clean_path(c.path).lower()}|{name}"


def load_state() -> dict[str, list]:
    try:
        data = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, list]) -> None:
    path = _state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# --- start and stop ---------------------------------------------------------

def start_smb_poll(config: AppConfig) -> bool:
    """Start the SMB poller (idempotent). Returns True if running."""
    global _thread, _error
    with _lock:
        if _thread and _thread.is_alive():
            return True
        try:
            import smbclient  # noqa: F401
        except ImportError:
            _error = "the smbprotocol package is not installed"
            logger.error("SMB poll could not start: %s", _error)
            return False
        reason = check_settings(config)
        if reason:
            _error = reason
            logger.error("SMB poll not started: %s", reason)
            return False
        _error = None
        _stop.clear()
        _thread = threading.Thread(target=_loop, args=(config,), daemon=True, name="smb-poll")
        _thread.start()
    logger.info("SMB poll started on %s (every %.0fs)", folder_unc(config),
                config.smb_poll.poll_interval)
    return True


def stop_smb_poll() -> None:
    """Stop the poller; joins within 5 s, safe if it never started."""
    global _thread
    with _lock:
        thread = _thread
        _stop.set()
    if thread:
        thread.join(timeout=5)
        logger.info("SMB poll stopped")
    with _lock:
        if _thread is thread:
            _thread = None
    try:
        import smbclient
        smbclient.reset_connection_cache()
    except Exception:
        pass


# --- polling ----------------------------------------------------------------

def _list_files(config: AppConfig) -> dict[str, tuple]:
    """Scan files in the folder: name -> (size, mtime)."""
    import smbclient
    out = {}
    for entry in smbclient.scandir(folder_unc(config), **_kw(config)):
        name = entry.name
        if name.startswith(".") or Path(name).suffix.lower() not in SCAN_EXTENSIONS:
            continue
        # Use the listing itself: entry.stat() would open a second session.
        info = entry.smb_info
        if entry.is_symlink() or not entry.is_file():
            continue
        out[name] = (int(info.end_of_file), int(info.last_write_time.timestamp()))
    return out


def poll_once(config: AppConfig, seen: dict[str, tuple], state: dict[str, list]) -> None:
    """One poll. `seen` is the listing of the previous poll (updated here)."""
    global _last_error
    try:
        _login(config)
        current = _list_files(config)
    except Exception as e:
        _last_error = _plain(e)
        logger.error("SMB poll could not read the share: %s", _last_error)
        seen.clear()
        return
    _last_error = None

    prefix = _state_key(config, "")
    for name, sig in current.items():
        if _stop.is_set():
            break
        key = _state_key(config, name)
        if state.get(key) == list(sig):
            # Handled already. Try again to clear it off the share.
            if seen.get(name) == sig:
                _finish(config, name)
            continue
        if seen.get(name) != sig:
            continue  # new or still changing; check again next poll
        if _deliver(config, name, sig):
            state[key] = list(sig)
            save_state(state)
            _finish(config, name)
    # Forget state of files that are gone from the share.
    gone = [k for k in state if k.startswith(prefix) and k[len(prefix):] not in current]
    if gone:
        for k in gone:
            del state[k]
        save_state(state)
    seen.clear()
    seen.update(current)


def _plain(e: Exception) -> str:
    return (str(e) or e.__class__.__name__).splitlines()[0][:200]


def _deliver(config: AppConfig, name: str, sig: tuple) -> bool:
    """Read and route one file. True when OpenScanHub owns it from now on
    (delivered, or held by the retry queue)."""
    import smbclient
    unc = folder_unc(config) + "\\" + name
    try:
        with smbclient.open_file(unc, mode="rb", **_kw(config)) as f:
            data = f.read()
    except Exception as e:
        logger.error("Could not read %s from the share: %s", name, _plain(e))
        return False
    if not data:
        return False
    if len(data) != sig[0]:
        return False  # grew while we read it; the next poll sees the new size
    filename = sanitize_filename(name)
    try:
        result = intake.handle_scan(config, data, source="smb_poll", started_by="smb poll",
                                    filename=filename)
    except Exception:
        logger.exception("Failed to route SMB file %s", name)
        _add_history(filename, len(data), None, "internal error")
        return False
    _add_history(result.filename or filename, result.size_bytes or len(data), result, None)
    if result.ok:
        logger.info("SMB scan routed: %s", filename)
        return True
    if intake.handled(result):
        logger.warning("SMB scan %s is in the retry queue: %s", filename, "; ".join(result.errors))
        return True
    logger.error("Could not deliver %s: %s", filename, "; ".join(result.errors))
    return False


def _finish(config: AppConfig, name: str) -> None:
    """Move the delivered file to processed/ or delete it."""
    import smbclient
    folder = folder_unc(config)
    src = folder + "\\" + name
    kw = _kw(config)
    try:
        if config.smb_poll.after_delivery == "delete":
            smbclient.remove(src, **_kw(config))
            return
        target_dir = folder + "\\" + PROCESSED_DIR
        try:
            smbclient.mkdir(target_dir, **kw)
        except Exception:
            if not smbclient.path.isdir(target_dir, **kw):
                raise
        dest = target_dir + "\\" + name
        if smbclient.path.exists(dest, **kw):
            stem, dot, ext = name.rpartition(".")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = target_dir + "\\" + (f"{stem}_{stamp}.{ext}" if dot else f"{name}_{stamp}")
        smbclient.rename(src, dest, **kw)
    except Exception as e:
        # The state file stops a second delivery; the next poll tries again.
        logger.error("Could not clear %s from the share: %s", name, _plain(e))


def _loop(config: AppConfig) -> None:
    interval = max(0.05, float(config.smb_poll.poll_interval))
    seen: dict[str, tuple] = {}
    state = load_state()
    while not _stop.is_set():
        try:
            poll_once(config, seen, state)
        except Exception as e:
            logger.error("SMB poll error: %s", _plain(e))
        _stop.wait(interval)


def _add_history(filename: str, size: int, result, error: Optional[str]) -> None:
    try:
        from ..web.dashboard_api import add_to_history
        add_to_history({
            "filename": filename,
            "timestamp": datetime.now().isoformat(),
            "size_bytes": size,
            "source": "smb_poll",
            "auto": True,
            "success": bool(result and result.ok),
            "results": result.to_dict() if result else {"ok": False, "saved_paths": [],
                                                        "errors": [error], "queued": False},
        })
    except Exception as e:
        logger.debug("Could not add SMB scan to history: %s", e)
