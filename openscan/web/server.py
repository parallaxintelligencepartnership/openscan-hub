"""HTTP server with URL routing for OpenScanHub web UI."""

import json
import logging
import mimetypes
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlparse

from ..config import AppConfig

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"

# Global config reference set by run_server
_config: Optional[AppConfig] = None

# Route registry: (method, path_prefix) -> handler function
_routes: dict[tuple[str, str], Callable] = {}


def route(method: str, path: str):
    """Decorator to register a route handler."""
    def decorator(func):
        _routes[(method.upper(), path)] = func
        return func
    return decorator


class RequestHandler(BaseHTTPRequestHandler):
    """HTTP request handler with routing support."""

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method: str):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)

        # Check exact route match first
        handler = _routes.get((method, path))
        if handler:
            try:
                handler(self, query)
            except Exception as e:
                logger.exception("Handler error for %s %s", method, path)
                self._send_json({"error": str(e)}, status=500)
            return

        # Check prefix matches for parameterized routes
        for (route_method, route_path), handler in _routes.items():
            if route_method == method and path.startswith(route_path) and route_path.endswith("/"):
                try:
                    handler(self, query, path[len(route_path):])
                except Exception as e:
                    logger.exception("Handler error for %s %s", method, path)
                    self._send_json({"error": str(e)}, status=500)
                return

        # Static files
        if method == "GET":
            self._serve_static(path)
            return

        self._send_error(404, "Not found")

    def _serve_static(self, path: str):
        """Serve static files from the static directory."""
        if path == "/":
            # Redirect to wizard or dashboard based on config
            if _config and _config.wizard_completed:
                self._redirect("/dashboard")
            else:
                self._redirect("/wizard")
            return

        if path == "/wizard":
            path = "/templates/wizard.html"
        elif path == "/dashboard":
            path = "/templates/dashboard.html"

        file_path = STATIC_DIR / path.lstrip("/")

        # Security: prevent directory traversal
        try:
            file_path = file_path.resolve()
            if not str(file_path).startswith(str(STATIC_DIR.resolve())):
                self._send_error(403, "Forbidden")
                return
        except (ValueError, OSError):
            self._send_error(400, "Bad request")
            return

        if file_path.is_file():
            content_type, _ = mimetypes.guess_type(str(file_path))
            content_type = content_type or "application/octet-stream"
            data = file_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(data)
        else:
            self._send_error(404, "Not found")

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def _send_json(self, data: Any, status: int = 200):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str):
        self._send_json({"error": message}, status=status)

    def read_json_body(self) -> dict:
        """Read and parse JSON request body."""
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        body = self.rfile.read(length)
        return json.loads(body.decode("utf-8"))

    def log_message(self, format, *args):
        logger.debug("%s %s", self.address_string(), format % args)


def get_config() -> AppConfig:
    """Get the current config. For use by route handlers."""
    assert _config is not None, "Server not initialized"
    return _config


def set_config(config: AppConfig) -> None:
    """Update the global config reference."""
    global _config
    _config = config


def run_server(config: AppConfig) -> None:
    """Start the HTTP server."""
    global _config
    _config = config

    # Import route modules to register their handlers
    from . import wizard_api  # noqa: F401
    from . import dashboard_api  # noqa: F401

    server = HTTPServer(("0.0.0.0", config.web_port), RequestHandler)
    logger.info("Web UI running at http://0.0.0.0:%d", config.web_port)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down web server")
        server.shutdown()
