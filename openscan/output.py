"""Output handlers for saving scan results."""

import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger(__name__)


def generate_filename(pattern: str, extension: str = ".pdf") -> str:
    """Generate a filename from a pattern with date/time placeholders."""
    now = datetime.now()
    name = pattern.replace("{date}", now.strftime("%Y-%m-%d"))
    name = name.replace("{time}", now.strftime("%H%M%S"))

    # Handle {n} counter - find next available number
    if "{n}" in name:
        name = name.replace("{n}", "{n_placeholder}")
        for n in range(1, 10000):
            candidate = name.replace("{n_placeholder}", str(n).zfill(3))
            # Caller will check if file exists; for now just use 001
            name = name.replace("{n_placeholder}", str(n).zfill(3))
            break

    if not name.endswith(extension):
        name += extension

    return name


def _unique_path(folder: Path, filename: str) -> Path:
    """Return a unique file path, appending _2, _3 etc. if needed."""
    path = folder / filename
    if not path.exists():
        return path

    stem = path.stem
    suffix = path.suffix
    counter = 2
    while True:
        candidate = folder / f"{stem}_{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def save_to_folder(
    pdf_data: bytes,
    folder: str,
    filename: str,
) -> Optional[str]:
    """Save scan data to a folder. Returns the saved file path."""
    try:
        folder_path = Path(folder)
        folder_path.mkdir(parents=True, exist_ok=True)
        file_path = _unique_path(folder_path, filename)
        file_path.write_bytes(pdf_data)
        logger.info("Saved scan to %s (%d bytes)", file_path, len(pdf_data))
        return str(file_path)
    except OSError as e:
        logger.error("Failed to save to %s: %s", folder, e)
        return None


def save_to_paperless_consume(
    pdf_data: bytes,
    consume_folder: str,
    filename: str,
) -> Optional[str]:
    """Save scan to Paperless-NGX consume folder (file drop method)."""
    return save_to_folder(pdf_data, consume_folder, filename)


def upload_to_paperless_api(
    pdf_data: bytes,
    api_url: str,
    api_token: str,
    filename: str,
    tags: Optional[list[str]] = None,
) -> Optional[dict]:
    """Upload scan to Paperless-NGX via REST API."""
    url = f"{api_url.rstrip('/')}/api/documents/post_document/"

    # Build multipart form data
    boundary = "----OpenScanHubBoundary"
    body = bytearray()

    # File field
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(
        f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'.encode()
    )
    body.extend(b"Content-Type: application/pdf\r\n\r\n")
    body.extend(pdf_data)
    body.extend(b"\r\n")

    # Title field
    title = Path(filename).stem
    body.extend(f"--{boundary}\r\n".encode())
    body.extend(b'Content-Disposition: form-data; name="title"\r\n\r\n')
    body.extend(title.encode())
    body.extend(b"\r\n")

    # Tags
    if tags:
        for tag in tags:
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(b'Content-Disposition: form-data; name="tags"\r\n\r\n')
            body.extend(tag.encode())
            body.extend(b"\r\n")

    body.extend(f"--{boundary}--\r\n".encode())

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Authorization": f"Token {api_token}",
    }

    try:
        req = Request(url, data=bytes(body), method="POST", headers=headers)
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            logger.info("Uploaded to Paperless: %s", result)
            return result
    except HTTPError as e:
        body_text = e.read().decode("utf-8", errors="replace")
        logger.error("Paperless API error %d: %s", e.code, body_text)
        return None
    except (URLError, OSError) as e:
        logger.error("Paperless API connection error: %s", e)
        return None


def test_paperless_connection(api_url: str, api_token: str) -> dict:
    """Test connection to Paperless-NGX API. Returns status dict."""
    url = f"{api_url.rstrip('/')}/api/"
    headers = {"Authorization": f"Token {api_token}"}

    try:
        req = Request(url, method="GET", headers=headers)
        with urlopen(req, timeout=10) as resp:
            return {"ok": True, "status": resp.status}
    except HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}", "status": e.code}
    except (URLError, OSError) as e:
        return {"ok": False, "error": str(e)}


def save_scan(
    pdf_data: bytes,
    output_folder: str,
    filename: str,
    paperless_consume: Optional[str] = None,
    paperless_api_url: Optional[str] = None,
    paperless_api_token: Optional[str] = None,
    paperless_tags: Optional[list[str]] = None,
) -> dict:
    """Save scan to all configured destinations. Returns results dict."""
    results = {"saved": [], "errors": []}

    # Always save to output folder
    if output_folder:
        path = save_to_folder(pdf_data, output_folder, filename)
        if path:
            results["saved"].append({"type": "folder", "path": path})
        else:
            results["errors"].append({"type": "folder", "error": "Failed to save"})

    # Paperless consume folder
    if paperless_consume:
        path = save_to_paperless_consume(pdf_data, paperless_consume, filename)
        if path:
            results["saved"].append({"type": "paperless_consume", "path": path})
        else:
            results["errors"].append({"type": "paperless_consume", "error": "Failed to save"})

    # Paperless API
    if paperless_api_url and paperless_api_token:
        resp = upload_to_paperless_api(
            pdf_data, paperless_api_url, paperless_api_token, filename, paperless_tags
        )
        if resp is not None:
            results["saved"].append({"type": "paperless_api", "response": resp})
        else:
            results["errors"].append({"type": "paperless_api", "error": "Upload failed"})

    return results
