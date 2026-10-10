"""Keep one OpenScanHub per user (review A7).

A lock file in the config folder records the pid and port of the running
instance. A second launch that finds a live pid there asks that port's
/api/health whether it really is OpenScanHub; only then does it open the
browser at the running UI instead of starting again. A pid that is alive but
does not answer as OpenScanHub (pids are reused, especially on Windows) makes
the lock stale: it is removed and this launch starts.
"""

import json
import logging
import os
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

LOCK_NAME = "openscan.lock"
HEALTH_TIMEOUT = 2.0
APP_ID = "openscanhub"


def _probe_host(bind: str) -> str:
    if not bind or bind in ("0.0.0.0", "::", "localhost") or bind.startswith("127."):
        return "127.0.0.1"
    return f"[{bind}]" if ":" in bind else bind


def answers_as_openscanhub(port: int, bind: str = "127.0.0.1",
                           timeout: float = HEALTH_TIMEOUT) -> bool:
    """True if http://<bind>:<port>/api/health answers as OpenScanHub."""
    if not port:
        return False
    url = f"http://{_probe_host(bind)}:{int(port)}/api/health"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(req, timeout=timeout) as resp:
            data = json.loads(resp.read(65536).decode("utf-8"))
    except Exception:
        return False
    if not isinstance(data, dict):
        return False
    if data.get("app") == APP_ID:
        return True
    # Versions before the "app" field: the health payload had these keys.
    return (data.get("status") == "ok" and isinstance(data.get("version"), str)
            and "queue_pending" in data)


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                    return False
                return code.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


class InstanceLock:
    def __init__(self, config_dir: Path):
        self.path = Path(config_dir) / LOCK_NAME
        self.held = False

    def read(self) -> Optional[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def running_instance(self) -> Optional[dict]:
        """Info of another live instance ({"pid", "port", "bind"}), or None."""
        data = self.read()
        if not data:
            return None
        try:
            pid = int(data.get("pid", 0))
        except (TypeError, ValueError):
            return None
        if pid == os.getpid() or not pid_alive(pid):
            return None
        try:
            port = int(data.get("port") or 0)
        except (TypeError, ValueError):
            port = 0
        if not answers_as_openscanhub(port, str(data.get("bind") or "127.0.0.1")):
            logger.info("Removing stale instance lock (pid %s is not OpenScanHub)", pid)
            try:
                self.path.unlink()
            except OSError:
                pass
            return None
        return data

    def acquire(self, port: int, bind: str = "127.0.0.1") -> Optional[dict]:
        """Take the lock. Returns the other instance's info if one is running.

        If the config folder is not writable the lock is skipped (logged) and
        None is returned so the app still starts.
        """
        other = self.running_instance()
        if other is not None:
            return other
        payload = json.dumps({"pid": os.getpid(), "port": port, "bind": bind})
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".lock.tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.path)
            self.held = True
        except OSError as e:
            logger.warning("Could not write the instance lock %s: %s", self.path, e)
        # Two launches at the same moment: whoever wrote last wins.
        data = self.read()
        if data and data.get("pid") not in (None, os.getpid()):
            try:
                if pid_alive(int(data["pid"])):
                    self.held = False
                    return data
            except (TypeError, ValueError):
                pass
        return None

    def release(self) -> None:
        if not self.held:
            return
        data = self.read()
        if data and data.get("pid") == os.getpid():
            try:
                self.path.unlink()
            except OSError:
                pass
        self.held = False
