"""SMB destination: write to a Windows or Samba share without a mount.

Uses smbprotocol (MIT, pure Python). The password is never logged.
"""

from __future__ import annotations

import errno
import logging
import os
import posixpath
import re
import threading
import uuid

from .base import DeliveryError, Destination, field
from .folder import _COUNTER_RE

logger = logging.getLogger(__name__)

_TIMEOUT = 15
_lock = threading.Lock()  # one SMB destination operation at a time


def _clean_parts(path: str) -> list[str]:
    parts = [p for p in re.split(r"[\\/]+", path or "") if p and p != "."]
    if ".." in parts:
        raise DeliveryError("The folder path on the share must not contain '..'.")
    return parts


def _candidates(filename: str):
    """Yield the file name, then collision-safe variants (same rules as the folder destination)."""
    yield filename
    stem, suffix = posixpath.splitext(filename)
    m = _COUNTER_RE.match(stem)
    if m:
        prefix, start = m.group(1), int(m.group(2)) + 1
        for n in range(start, start + 1000):
            yield f"{prefix}{str(n).zfill(3)}{suffix}"
    else:
        for n in range(2, 1000):
            yield f"{stem}_{n}{suffix}"


def _settings(options: dict) -> dict:
    server = str(options.get("server") or "").strip().strip("\\/")
    share = str(options.get("share") or "").strip().strip("\\/")
    if not server:
        raise DeliveryError("No SMB server is set.")
    if not share:
        raise DeliveryError("No share name is set.")
    user = str(options.get("username") or "")
    domain = str(options.get("domain") or "").strip()
    if user and domain and "\\" not in user and "@" not in user:
        user = f"{domain}\\{user}"
    port = 445
    if ":" in server and server.count(":") == 1:
        server, p = server.split(":")
        try:
            port = int(p)
        except ValueError:
            raise DeliveryError("The SMB server port must be a number.") from None
    return {"server": server, "share": share, "port": port, "username": user or None,
            "password": str(options.get("password") or "") or None,
            "parts": _clean_parts(str(options.get("path") or ""))}


def _unc(s: dict, *extra: str) -> str:
    return "\\\\" + "\\".join([s["server"], s["share"], *s["parts"], *extra])


def _status_hex(e: Exception) -> str:
    code = getattr(e, "ntstatus", None)
    return f"0x{code:08x}" if isinstance(code, int) else ""


def _smb_error(e: Exception, s: dict) -> str:
    """A plain sentence for an smbprotocol or socket failure. Never includes the password."""
    name = type(e).__name__
    text = str(e)
    where = f"\\\\{s['server']}\\{s['share']}"
    low = text.lower()
    code = getattr(e, "ntstatus", None)
    if isinstance(code, int):
        text += f" 0x{code:08x}"
        low = text.lower()
    if "0xc000006d" in low or "0xc000006a" in low or "logon" in low or "authentication" in low or "SpnegoError" in name or "LogonFailure" in name:
        return f"The SMB server rejected the user name or password for {s['server']}."
    if "BAD_NETWORK_NAME" in text or "0xc00000cc" in low or "0xc0000225" in low or "bad network name" in low:
        return f"The share {where} was not found on the server. Check the share name."
    if "ACCESS_DENIED" in text or "0xc0000022" in low:
        return f"The user is not allowed to write to {where}. Check the share permissions."
    if "DISK_FULL" in text or "0xc000007f" in low:
        return f"There is no free space left on {where}."
    if isinstance(e, (TimeoutError, ConnectionError, OSError)) or "connect" in low or "timed out" in low:
        return f"Could not reach the SMB server {s['server']}. Check that it is online and the name is correct."
    return f"Could not save to {where}: {name}."


def _kw(s: dict) -> dict:
    """Connection arguments every smbclient call needs, with this operation's
    own connection cache."""
    return {"username": s["username"], "password": s["password"], "port": s["port"],
            "connection_timeout": _TIMEOUT, "connection_cache": s.get("cache")}


def _session(s: dict):
    import smbclient
    # A private connection cache per operation: new credentials always count,
    # nothing stays open, and the SMB poll's sessions in the global cache are
    # not touched.
    s["cache"] = {}
    smbclient.register_session(s["server"], **_kw(s))
    return smbclient


def _close(s: dict) -> None:
    cache = s.get("cache")
    if cache is None:
        return
    try:
        import smbclient
        smbclient.reset_connection_cache(connection_cache=cache)
    except Exception:  # closing must never hide the real result
        pass


def _ensure_dirs(smbclient, s: dict) -> None:
    for i in range(1, len(s["parts"]) + 1):
        d = "\\\\" + "\\".join([s["server"], s["share"], *s["parts"][:i]])
        try:
            smbclient.stat(d, **_kw(s))
        except OSError:
            smbclient.mkdir(d, **_kw(s))


class SmbDestination(Destination):
    type = "smb"
    label = "SMB network share"
    feature = None
    target_key = "server"
    FIELDS = [
        field("server", "Server name or address", "text", required=True),
        field("share", "Share name", "text", required=True),
        field("path", "Folder on the share", "path"),
        field("username", "User name", "text"),
        field("password", "Password", "password"),
        field("domain", "Domain or workgroup", "text"),
    ]

    def deliver(self, pdf: bytes, filename: str, options: dict) -> tuple[str, bool]:
        with _lock:
            return self._deliver(pdf, filename, options)

    def _deliver(self, pdf: bytes, filename: str, options: dict) -> tuple[str, bool]:
        s = _settings(options)
        base = os.path.basename(filename.replace("\\", "/")) or "scan.pdf"
        try:
            smbclient = _session(s)
            _ensure_dirs(smbclient, s)
            for name in _candidates(base):
                try:
                    f = smbclient.open_file(_unc(s, name), mode="xb", **_kw(s))
                except OSError as e:
                    if e.errno == errno.EEXIST:
                        continue
                    raise
                with f:
                    f.write(pdf)
                break
            else:
                raise DeliveryError("Could not find a free file name on the share.")
        except DeliveryError:
            raise
        except Exception as e:  # smbprotocol raises many types
            logger.error("SMB delivery to %s failed: %s %s", s["server"], type(e).__name__,
                         _status_hex(e))
            raise DeliveryError(_smb_error(e, s)) from e
        finally:
            _close(s)
        location = _unc(s, name)
        logger.info("Saved scan to %s (%d bytes)", location, len(pdf))
        return location, True

    def test(self, options: dict) -> dict:
        with _lock:
            return self._test(options)

    def _test(self, options: dict) -> dict:
        try:
            s = _settings(options)
        except DeliveryError as e:
            return {"ok": False, "message": str(e)}
        try:
            smbclient = _session(s)
            _ensure_dirs(smbclient, s)
            probe = _unc(s, f".openscan-test-{uuid.uuid4().hex[:8]}.tmp")
            with smbclient.open_file(probe, mode="xb", **_kw(s)) as f:
                f.write(b"openscan")
            smbclient.remove(probe, **_kw(s))
        except DeliveryError as e:
            return {"ok": False, "message": str(e)}
        except Exception as e:
            logger.error("SMB test for %s failed: %s %s", s["server"], type(e).__name__,
                         _status_hex(e))
            return {"ok": False, "message": _smb_error(e, s)}
        finally:
            _close(s)
        return {"ok": True, "message": f"OpenScanHub can write to \\\\{s['server']}\\{s['share']}."}
