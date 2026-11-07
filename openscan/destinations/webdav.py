"""WebDAV destination: any WebDAV server, including Nextcloud and ownCloud.

Uses the standard library only. Certificates are checked unless the user
allows a self-signed one. The password is never logged.
"""

from __future__ import annotations

import base64
import logging
import os
import posixpath
import socket
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPSHandler, Request, build_opener, HTTPRedirectHandler

from .base import DeliveryError, Destination, field
from .smb import _candidates

logger = logging.getLogger(__name__)

_TIMEOUT = 30


class _NoRedirect(HTTPRedirectHandler):
    """Do not follow redirects: a redirect would resend the password and change PUT to GET."""

    def redirect_request(self, *args, **kwargs):
        return None


def _opener(verify: bool):
    ctx = ssl.create_default_context()
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return build_opener(HTTPSHandler(context=ctx), _NoRedirect())


def _base(options: dict) -> str:
    url = str(options.get("url") or "").strip()
    if not url:
        raise DeliveryError("No WebDAV address is set.")
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise DeliveryError("The WebDAV address must start with https:// (or http://).")
    return url.rstrip("/")


def _headers(options: dict, extra: dict | None = None) -> dict:
    h = dict(extra or {})
    user = str(options.get("username") or "")
    if user:
        raw = f"{user}:{options.get('password') or ''}".encode("utf-8")
        h["Authorization"] = "Basic " + base64.b64encode(raw).decode("ascii")
    return h


def _request(method: str, url: str, options: dict, data: bytes | None = None,
             headers: dict | None = None):
    req = Request(url, data=data, method=method, headers=_headers(options, headers))
    verify = not options.get("allow_self_signed")
    return _opener(verify).open(req, timeout=_TIMEOUT)


def _status(method: str, url: str, options: dict, data: bytes | None = None,
            headers: dict | None = None) -> int:
    """Run a request and return the status code. HTTP error codes are returned, not raised."""
    try:
        with _request(method, url, options, data, headers) as r:
            return r.status
    except HTTPError as e:
        return e.code


def _error(e: Exception, base: str) -> str:
    host = urlsplit(base).netloc
    if isinstance(e, HTTPError):
        return _http_message(e.code, host)
    reason = getattr(e, "reason", e)
    if isinstance(reason, ssl.SSLCertVerificationError) or isinstance(e, ssl.SSLCertVerificationError):
        return (f"The certificate of {host} is not trusted. Fix the certificate "
                "or turn on 'Allow a self-signed certificate'.")
    if isinstance(reason, ssl.SSLError):
        return f"Could not make a secure connection to {host}."
    if isinstance(reason, (socket.timeout, TimeoutError)) or isinstance(e, TimeoutError):
        return f"{host} did not answer in time."
    return f"Could not reach {host}. Check that it is online and the address is correct."


def _http_message(code: int, host: str) -> str:
    if code == 401:
        return f"{host} rejected the user name or password."
    if code == 403:
        return f"The user is not allowed to write to this folder on {host}."
    if code == 404:
        return f"The folder was not found on {host}. Check the address."
    if code == 507:
        return f"There is no free space left on {host}."
    if code in (301, 302, 307, 308):
        return f"{host} redirected the request. Use the exact WebDAV address."
    return f"{host} answered with error {code}."


def _join(base: str, parts: list[str]) -> str:
    return "/".join([base, *[quote(p, safe="") for p in parts]])


def _folder_parts(options: dict) -> list[str]:
    parts = [p for p in str(options.get("path") or "").replace("\\", "/").split("/") if p and p != "."]
    if ".." in parts:
        raise DeliveryError("The folder path must not contain '..'.")
    return parts


def _ensure_folders(base: str, parts: list[str], options: dict) -> None:
    for i in range(1, len(parts) + 1):
        url = _join(base, parts[:i])
        code = _status("MKCOL", url, options)
        # 201 created; 405 already exists; 301 some servers answer for an existing folder
        if code in (200, 201, 204, 405, 301):
            continue
        raise DeliveryError(_http_message(code, urlsplit(base).netloc))


class WebdavDestination(Destination):
    type = "webdav"
    label = "WebDAV or Nextcloud"
    feature = None
    target_key = "url"
    FIELDS = [
        field("url", "WebDAV address", "text", required=True),
        field("path", "Folder", "path"),
        field("username", "User name", "text"),
        field("password", "Password or app password", "password"),
        field("allow_self_signed", "Allow a self-signed certificate", "bool"),
    ]

    def deliver(self, pdf: bytes, filename: str, options: dict) -> tuple[str, bool]:
        base = _base(options)
        parts = _folder_parts(options)
        name0 = os.path.basename(filename.replace("\\", "/")) or "scan.pdf"
        try:
            _ensure_folders(base, parts, options)
            for name in _candidates(name0):
                url = _join(base, [*parts, name])
                # If-None-Match: * asks the server not to overwrite an existing file.
                code = _status("PUT", url, options, pdf,
                               {"Content-Type": "application/pdf", "If-None-Match": "*"})
                if code in (200, 201, 204):
                    break
                if code in (409, 412) or (code == 405 and name != name0):
                    # Name taken (412), or a parent is missing (409, not retried)
                    if code == 409:
                        raise DeliveryError("The folder does not exist on the server and could not be created.")
                    continue
                raise DeliveryError(_http_message(code, urlsplit(base).netloc))
            else:
                raise DeliveryError("Could not find a free file name on the server.")
        except DeliveryError:
            raise
        except (URLError, OSError) as e:
            logger.error("WebDAV delivery to %s failed: %s", urlsplit(base).netloc, type(e).__name__)
            raise DeliveryError(_error(e, base)) from e
        logger.info("Uploaded scan to %s (%d bytes)", urlsplit(url).netloc, len(pdf))
        return posixpath.join(base, *[quote(p, safe="") for p in [*parts, name]]), True

    def test(self, options: dict) -> dict:
        try:
            base = _base(options)
            parts = _folder_parts(options)
            url = _join(base, parts)
            code = _status("PROPFIND", url, options, None, {"Depth": "0"})
            if code in (207, 200):
                return {"ok": True, "message": f"OpenScanHub can reach {urlsplit(base).netloc}."}
            if code == 404 and parts:
                # The folder is created on first delivery; the server itself answered.
                code2 = _status("PROPFIND", base, options, None, {"Depth": "0"})
                if code2 in (207, 200):
                    return {"ok": True, "message": "The server works. The folder will be created on the first scan."}
            return {"ok": False, "message": _http_message(code, urlsplit(base).netloc)}
        except DeliveryError as e:
            return {"ok": False, "message": str(e)}
        except (URLError, OSError) as e:
            return {"ok": False, "message": _error(e, str(options.get("url") or ""))}
