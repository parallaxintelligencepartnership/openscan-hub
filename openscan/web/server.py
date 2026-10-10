"""HTTP server with URL routing, authentication and request hardening.

Security model (review finding C1):
- Binds to ``config.web.bind`` (127.0.0.1 unless the user allows LAN access).
- ``/api/health`` and the login page are open. Every other ``/api/`` route needs
  the access token (``X-OpenScan-Token`` or ``Authorization: Bearer``) or the
  ``openscan_session`` cookie, whose value is an HMAC of the token.
- Browsers on the same machine get the cookie on their first page load. Other
  devices sign in once on ``/login``.
- The Host header is checked against an allowlist (DNS rebinding defense).
- POSTs need ``Content-Type: application/json`` and, if an Origin header is
  present, it must match the Host (CSRF defense).
"""

import hashlib
import hmac
import ipaddress
import json
import logging
import mimetypes
import os
import re
import socket
import threading
from dataclasses import dataclass
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from ..config import AppConfig

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

SESSION_COOKIE = "openscan_session"
SESSION_MAX_AGE = 30 * 24 * 3600
MAX_BODY_BYTES = 1024 * 1024

# Pages that need a signed-in browser. Assets (css/js) are open.
PAGES = {
    "/dashboard": "templates/dashboard.html",
    "/wizard": "templates/wizard.html",
    "/settings": "templates/settings.html",
}
LOGIN_PAGE = "templates/login.html"

_config: Optional[AppConfig] = None
_config_lock = threading.Lock()


@dataclass
class Route:
    handler: Callable
    public: bool = False  # no auth at all
    query_token: bool = False  # accept ?token=
    cookie: bool = True  # accept the session cookie
    json_only: bool = True  # POST body must be application/json
    guard: Optional[Callable[[AppConfig], bool]] = None  # route exists only if True


_routes: dict[tuple[str, str], Route] = {}


def route(method: str, path: str, *, public: bool = False, query_token: bool = False,
          cookie: bool = True, json_only: bool = True,
          guard: Optional[Callable[[AppConfig], bool]] = None):
    """Decorator to register a route handler.

    A path ending in "/" is a prefix route; the handler gets the remainder.
    """
    def decorator(func):
        _routes[(method.upper(), path)] = Route(func, public, query_token, cookie, json_only, guard)
        return func
    return decorator


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def session_value(token: str) -> str:
    """Cookie value for a token. The token itself never goes into the cookie."""
    return hmac.new(token.encode("utf-8"), b"openscan-session-v1", hashlib.sha256).hexdigest()


_TOKEN_IN_URL = re.compile(r"([?&])token=[^&\s\"]*")

# Headers a reverse proxy adds. A loopback peer sending any of them is a proxy
# forwarding someone else's request.
PROXY_HEADERS = ("X-Forwarded-For", "X-Real-IP", "Forwarded", "X-Forwarded-Host")


def secret_equal(supplied: str, expected: str) -> bool:
    """Constant-time compare that never raises (compare_digest refuses
    non-ASCII str, which used to drop the connection)."""
    return hmac.compare_digest(supplied.encode("utf-8", "surrogatepass"),
                               expected.encode("utf-8", "surrogatepass"))


def _is_loopback(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if getattr(addr, "ipv4_mapped", None):
        addr = addr.ipv4_mapped
    return addr.is_loopback


def is_loopback_bind(bind: str) -> bool:
    return bind == "localhost" or _is_loopback(bind)


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _trusted_proxies() -> set[str]:
    raw = os.environ.get("OPENSCAN_TRUST_PROXY_IP", "")
    return {p.strip() for p in raw.split(",") if p.strip()}


def _extra_allowed_hosts() -> set[str]:
    raw = os.environ.get("OPENSCAN_ALLOWED_HOSTS", "")
    return {h.strip().lower() for h in raw.split(",") if h.strip()}


_own_names_cache: Optional[set[str]] = None


def _own_names() -> set[str]:
    """Hostnames and IP addresses that belong to this machine."""
    global _own_names_cache
    if _own_names_cache is not None:
        return _own_names_cache
    names = {"localhost", "127.0.0.1", "::1", "openscanhub.local"}
    try:
        hostname = socket.gethostname()
        if hostname:
            names.add(hostname.lower())
            names.add(hostname.split(".")[0].lower())
            names.add(hostname.split(".")[0].lower() + ".local")
        for info in socket.getaddrinfo(hostname, None):
            names.add(str(info[4][0]).split("%")[0].lower())
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # no packets are sent for UDP connect
            names.add(s.getsockname()[0])
    except OSError:
        pass
    _own_names_cache = names
    return names


def _split_host(host_header: str) -> str:
    host = host_header.strip().lower()
    if host.startswith("["):
        end = host.find("]")
        return host[1:end] if end > 0 else host
    if host.count(":") == 1:
        host = host.split(":", 1)[0]
    return host.rstrip(".")


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler with routing, auth and request checks."""

    server_version = "OpenScanHub"
    sys_version = ""
    timeout = 30  # seconds; an idle socket can no longer hold a worker forever

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    # --- request facts -------------------------------------------------

    def peer_ip(self) -> str:
        override = getattr(self.server, "peer_override", None)
        if override:
            return override
        return str(self.client_address[0])

    def is_loopback_peer(self) -> bool:
        """True for a browser on this computer that reached us directly.

        A reverse proxy on the same machine also connects from loopback, so a
        request carrying any proxy header counts as remote."""
        if not _is_loopback(self.peer_ip()):
            return False
        return not any(self.headers.get(h) for h in PROXY_HEADERS)

    def may_auto_sign_in(self) -> bool:
        if os.environ.get("OPENSCAN_DISABLE_LOCAL_AUTOLOGIN", "").strip() not in ("", "0"):
            return False
        return self.is_loopback_peer()

    def is_trusted_proxy(self) -> bool:
        return self.peer_ip() in _trusted_proxies()

    def ingress_prefix(self) -> str:
        prefix = (self.headers.get("X-Ingress-Path") or "").strip()
        if not prefix.startswith("/") or "//" in prefix or "\\" in prefix:
            return ""
        return prefix.rstrip("/")

    def _host_allowed(self) -> bool:
        host_header = self.headers.get("Host")
        if not host_header:
            return True  # not a browser; rebinding needs a Host header
        host = _split_host(host_header)
        if host in _own_names() or host in _extra_allowed_hosts():
            return True
        try:
            if host == str(self.connection.getsockname()[0]).lower():
                return True
        except OSError:
            pass
        bind = get_config().web.bind if _config else "127.0.0.1"
        if not is_loopback_bind(bind):
            # LAN mode: a bare IP or a .local name is fine. A DNS rebinding
            # attack needs a real domain name, which is what we refuse.
            if _is_ip(host) or host.endswith(".local"):
                return True
        return False

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        host = (self.headers.get("Host") or "").strip().lower()
        try:
            netloc = urlparse(origin).netloc.lower()
        except ValueError:
            return False
        return bool(netloc) and netloc == host

    def _cookie_ok(self) -> bool:
        raw = self.headers.get("Cookie")
        if not raw:
            return False
        try:
            jar = SimpleCookie()
            jar.load(raw)
        except Exception:
            return False
        morsel = jar.get(SESSION_COOKIE)
        if morsel is None:
            return False
        return secret_equal(morsel.value, session_value(get_config().web.api_token))

    def token_ok(self, query: Optional[dict] = None) -> bool:
        token = get_config().web.api_token
        if not token:
            return False
        supplied = self.headers.get("X-OpenScan-Token")
        if not supplied:
            auth = self.headers.get("Authorization", "")
            if auth.lower().startswith("bearer "):
                supplied = auth[7:].strip()
        if not supplied and query is not None:
            supplied = (query.get("token") or [""])[0]
        return bool(supplied) and secret_equal(supplied, token)

    def _authorized(self, query: dict, rt: Route) -> bool:
        if self.is_trusted_proxy():
            return True
        if self.token_ok(query if rt.query_token else None):
            return True
        return rt.cookie and self._cookie_ok()

    def session_cookie_header(self) -> str:
        value = session_value(get_config().web.api_token)
        return (f"{SESSION_COOKIE}={value}; Path=/; Max-Age={SESSION_MAX_AGE}; "
                "HttpOnly; SameSite=Strict")

    # --- dispatch --------------------------------------------------------

    def _match(self, method: str, path: str) -> tuple[Optional[Route], Optional[str]]:
        rt = _routes.get((method, path))
        if rt:
            return rt, None
        for (m, p), candidate in _routes.items():
            if m == method and p.endswith("/") and path.startswith(p):
                return candidate, path[len(p):]
        return None, None

    def _path_exists_other_method(self, method: str, path: str) -> bool:
        for (m, p), rt in _routes.items():
            if rt.guard is not None and not rt.guard(get_config()):
                continue
            if m != method and (p == path or (p.endswith("/") and path.startswith(p))):
                return True
        return False

    def _handle(self, method: str):
        self._extra_headers: list[tuple[str, str]] = []
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        trusted = self.is_trusted_proxy()
        if not trusted and not self._host_allowed():
            self._send_error(403, "This address is not allowed. Open OpenScanHub by its IP "
                                  "address, or add the name to OPENSCAN_ALLOWED_HOSTS.")
            return

        if path == "/api" or path.startswith("/api/"):
            self._handle_api(method, path, query, trusted)
            return

        if method == "GET":
            self._serve_page_or_static(path, query)
            return
        self._send_error(404, "Not found")

    def _handle_api(self, method: str, path: str, query: dict, trusted: bool):
        rt, suffix = self._match(method, path)
        if rt is not None and rt.guard is not None and not rt.guard(get_config()):
            rt = None
        if rt is None:
            if self._path_exists_other_method(method, path):
                self._send_error(405, "Method not allowed")
            else:
                self._send_error(404, "Not found")
            return

        if method == "POST" and not trusted and not self._origin_ok():
            self._send_error(403, "Cross-site request refused")
            return
        if not rt.public and not self._authorized(query, rt):
            self._send_error(401, "Sign in required")
            return
        if method == "POST" and rt.json_only:
            ctype = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
            if ctype != "application/json":
                self._send_error(415, "Content-Type must be application/json")
                return

        try:
            if suffix is None:
                rt.handler(self, query)
            else:
                rt.handler(self, query, suffix)
        except BadRequest as e:
            self._send_error(e.status, str(e))
        except Exception as e:
            logger.exception("Handler error for %s %s", method, path)
            self._send_error(500, str(e))

    def _serve_page_or_static(self, path: str, query: dict):
        if path == "/login":
            if self.may_auto_sign_in() or self.is_trusted_proxy() or self._cookie_ok():
                self._issue_cookie_if_needed()
                self._redirect("dashboard")
                return
            self._serve_file(STATIC_DIR / LOGIN_PAGE)
            return

        if path == "/" or path in PAGES:
            if not (self.is_trusted_proxy() or self._cookie_ok() or self.token_ok()):
                if self.may_auto_sign_in():
                    self._extra_headers.append(("Set-Cookie", self.session_cookie_header()))
                else:
                    self._redirect("login")
                    return
            if path == "/":
                cfg = get_config()
                self._redirect("dashboard" if cfg.wizard_completed else "wizard")
                return
            self._serve_file(STATIC_DIR / PAGES[path])
            return

        if path.startswith("/templates/"):
            self._send_error(404, "Not found")
            return

        file_path = STATIC_DIR / path.lstrip("/")
        try:
            file_path = file_path.resolve()
            static_root = STATIC_DIR.resolve()
            if file_path != static_root and static_root not in file_path.parents:
                self._send_error(403, "Forbidden")
                return
        except (ValueError, OSError):
            self._send_error(400, "Bad request")
            return
        self._serve_file(file_path)

    def _issue_cookie_if_needed(self):
        if not self._cookie_ok():
            self._extra_headers.append(("Set-Cookie", self.session_cookie_header()))

    # --- responses -------------------------------------------------------

    def _common_headers(self):
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "frame-ancestors 'self'")
        for name, value in getattr(self, "_extra_headers", []):
            self.send_header(name, value)

    def add_header(self, name: str, value: str) -> None:
        """Queue an extra response header (e.g. Set-Cookie) for the next response."""
        self._extra_headers.append((name, value))

    def _serve_file(self, file_path: Path):
        if not file_path.is_file():
            self._send_error(404, "Not found")
            return
        content_type, _ = mimetypes.guess_type(str(file_path))
        content_type = content_type or "application/octet-stream"
        if content_type.startswith("text/") or content_type.endswith("javascript"):
            content_type += "; charset=utf-8"
        data = file_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self._common_headers()
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, location: str):
        """Redirect to a page. Relative targets keep Home Assistant ingress working."""
        prefix = self.ingress_prefix()
        if prefix:
            location = f"{prefix}/{location.lstrip('/')}"
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self._common_headers()
        self.end_headers()

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._common_headers()
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str):
        self._send_json({"error": message}, status=status)

    def read_json_body(self) -> dict:
        """Read and parse a JSON object body. Raises BadRequest on bad input."""
        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
        except ValueError:
            raise BadRequest("Bad Content-Length")
        if length <= 0:
            return {}
        if length > MAX_BODY_BYTES:
            raise BadRequest("Request too large", status=413)
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise BadRequest("Body is not valid JSON")
        if not isinstance(data, dict):
            raise BadRequest("Body must be a JSON object")
        return data

    def log_message(self, format, *args):
        logger.debug("%s %s", self.peer_ip(), _TOKEN_IN_URL.sub(r"\1token=<hidden>", format % args))


class OpenScanServer(ThreadingHTTPServer):
    daemon_threads = True
    # On Windows SO_REUSEADDR lets a second process bind the same port (A7).
    allow_reuse_address = os.name != "nt"
    peer_override: Optional[str] = None  # tests only: pretend requests come from this IP


def get_config() -> AppConfig:
    """Get the current config. For use by route handlers."""
    assert _config is not None, "Server not initialized"
    return _config


def set_config(config: AppConfig) -> None:
    """Replace the live config."""
    global _config
    with _config_lock:
        _config = config


def load_routes() -> None:
    """Import route modules so their handlers register."""
    from . import wizard_api  # noqa: F401
    from . import dashboard_api  # noqa: F401
    from . import settings_api  # noqa: F401
    from ..integrations import homeassistant
    homeassistant.register()


def create_server(config: AppConfig) -> OpenScanServer:
    """Bind the HTTP server. Raises OSError if the port is taken."""
    set_config(config)
    load_routes()
    server = OpenScanServer((config.web.bind, config.web.port), RequestHandler)
    logger.info("Web UI listening on http://%s:%d", config.web.bind, server.server_address[1])
    return server


def run_server(config: AppConfig) -> None:
    """Bind and serve until interrupted (kept for callers of the old API)."""
    server = create_server(config)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down web server")
    finally:
        server.server_close()
