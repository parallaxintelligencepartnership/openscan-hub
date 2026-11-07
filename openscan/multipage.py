"""Multi-page scanning session management.

A session is not a scan: it collects pages the user scans one at a time.
Each page scan takes the global scanner lock for its own duration only
(`scan_page`), so the ADF monitor cannot interleave with it. While any
session has recent activity the monitor does not start auto-scans.
"""

import hashlib
import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import BytesIO
from typing import Callable, Optional

from PIL import Image, ImageOps
from pypdf import PdfReader, PdfWriter

from . import pages
from .scanner.base import ScanSource, release_scan, try_acquire_scan

logger = logging.getLogger(__name__)

# A session with no activity for this long is discarded.
SESSION_MAX_AGE = timedelta(minutes=30)
# The ADF monitor pauses while a session was used within this window.
MONITOR_PAUSE_WINDOW = timedelta(minutes=10)
# Upper bound on concurrent sessions (oldest is evicted).
MAX_SESSIONS = 4
# How long a page scan waits for the scanner lock before reporting busy.
PAGE_LOCK_TIMEOUT = 2.0
# Largest single upload (one photo or one PDF), in bytes.
MAX_UPLOAD_BYTES = 25 * 1024 * 1024
# Upper bound on pages in one session.
MAX_PAGES = 300


class ScannerBusyError(RuntimeError):
	"""The scanner is in use by another scan (manual or automatic)."""

	def __init__(self, message: str = "The scanner is busy with another scan. Try again in a moment."):
		super().__init__(message)


@dataclass
class MultiPageSession:
	session_id: str
	pages: list[bytes] = field(default_factory=list)
	created_at: datetime = field(default_factory=datetime.now)
	last_activity: datetime = field(default_factory=datetime.now)
	source: ScanSource = ScanSource.FLATBED
	profile_id: str = ""


_sessions: dict[str, MultiPageSession] = {}
_sessions_lock = threading.RLock()


def ensure_pdf(data: bytes, resolution: int = 300) -> bytes:
	"""Convert image data to PDF if needed. Returns PDF bytes."""
	return pages.ensure_pdf(data, resolution)


def create_session(source: ScanSource, profile_id: str = "") -> str:
	"""Create a new multi-page session."""
	session_id = str(uuid.uuid4())
	with _sessions_lock:
		_purge_locked()
		while len(_sessions) >= MAX_SESSIONS:
			oldest = min(_sessions.values(), key=lambda s: s.last_activity)
			del _sessions[oldest.session_id]
			logger.info('Evicted multipage session %s (too many sessions)', oldest.session_id)
		_sessions[session_id] = MultiPageSession(session_id=session_id, source=source,
		                                          profile_id=profile_id)
	logger.info('Created multipage session %s for %s', session_id, source.value)
	return session_id


def get_session(session_id: str) -> Optional[MultiPageSession]:
	"""Get a session by ID (None if missing or expired)."""
	with _sessions_lock:
		_purge_locked()
		return _sessions.get(session_id)


def active_session_count() -> int:
	"""Sessions used recently enough that auto-scanning should pause."""
	now = datetime.now()
	with _sessions_lock:
		return sum(1 for s in _sessions.values() if now - s.last_activity <= MONITOR_PAUSE_WINDOW)


def add_page(session_id: str, scan_data: bytes) -> int:
	"""Add a page (PDF or image bytes) to the session. Returns new page count."""
	pdf_bytes = pages.ensure_pdf(scan_data)
	with _sessions_lock:
		session = _sessions.get(session_id)
		if not session:
			raise ValueError(f'Session not found: {session_id}')
		session.pages.append(pdf_bytes)
		session.last_activity = datetime.now()
		count = len(session.pages)
	logger.info('Added page to session %s, now %d pages', session_id, count)
	return count


def scan_page(session_id: str, scan_fn: Callable[[], bytes], lock_timeout: float = PAGE_LOCK_TIMEOUT) -> int:
	"""Scan one page with the scanner lock held and add it to the session.

	`scan_fn` performs the actual scan and returns PDF or image bytes.
	Raises ScannerBusyError when another scan holds the lock and
	ValueError when the session does not exist.
	"""
	if get_session(session_id) is None:
		raise ValueError(f'Session not found: {session_id}')
	if not try_acquire_scan(lock_timeout):
		raise ScannerBusyError()
	try:
		_touch(session_id)
		data = scan_fn()
	finally:
		release_scan()
	return add_page(session_id, data)


def _touch(session_id: str) -> None:
	with _sessions_lock:
		s = _sessions.get(session_id)
		if s:
			s.last_activity = datetime.now()


def merge_pages(session_id: str, remove_blank_pages: bool = False) -> bytes:
	"""Merge all pages in session into a single PDF."""
	with _sessions_lock:
		session = _sessions.get(session_id)
		if not session:
			raise ValueError(f'Session not found: {session_id}')
		snapshot = list(session.pages)
	if not snapshot:
		raise ValueError('No pages to merge')
	merged = pages.combine_pages(snapshot)
	if remove_blank_pages:
		merged = pages.finish_scan(merged, remove_blank_pages=True)
	return merged


def delete_session(session_id: str) -> None:
	"""Delete a session."""
	with _sessions_lock:
		if _sessions.pop(session_id, None) is not None:
			logger.info('Deleted multipage session %s', session_id)


def _purge_locked() -> int:
	now = datetime.now()
	stale = [sid for sid, s in _sessions.items() if now - s.last_activity > SESSION_MAX_AGE]
	for sid in stale:
		del _sessions[sid]
	if stale:
		logger.info('Cleaned up %d stale multipage sessions', len(stale))
	return len(stale)


def cleanup_stale_sessions() -> int:
	"""Remove sessions idle longer than max age. Returns count removed."""
	with _sessions_lock:
		return _purge_locked()


def generate_thumbnail(pdf_bytes: bytes, width: int = 120) -> bytes:
	"""Render the first page of a PDF as a JPEG thumbnail `width` px wide."""
	return pages.thumbnail_bytes(pdf_bytes, 0, width=width)


def get_page_thumbnail(session_id: str, page_index: int) -> bytes:
	"""Get JPEG thumbnail for a specific page in session."""
	with _sessions_lock:
		session = _sessions.get(session_id)
		if not session:
			raise ValueError(f'Session not found: {session_id}')
		if page_index < 0 or page_index >= len(session.pages):
			raise IndexError(f'Page index out of range: {page_index}')
		data = session.pages[page_index]
	return generate_thumbnail(data)


def get_session_info(session_id: str) -> Optional[dict]:
	"""Get session info as dict."""
	with _sessions_lock:
		session = _sessions.get(session_id)
		if not session:
			return None
		return {
			'session_id': session.session_id,
			'page_count': len(session.pages),
			'source': session.source.value,
			'profile_id': session.profile_id,
			'created_at': session.created_at.isoformat(),
			# A short id per page. It changes when the page image changes
			# (for example after a rotation), so a client can cache thumbnails.
			'pages': [hashlib.sha1(p).hexdigest()[:12] for p in session.pages],
		}


# --- editing: reorder, rotate, delete, upload ------------------------------------

class UploadError(ValueError):
	"""An uploaded file cannot be used as a page. The message is safe to show."""


def _editable(session_id: str) -> MultiPageSession:
	session = _sessions.get(session_id)
	if not session:
		raise ValueError(f'Session not found: {session_id}')
	return session


def _check_index(session: MultiPageSession, page_index) -> int:
	if isinstance(page_index, bool) or not isinstance(page_index, int):
		raise IndexError('Page index must be a whole number')
	if page_index < 0 or page_index >= len(session.pages):
		raise IndexError(f'Page index out of range: {page_index}')
	return page_index


def reorder_pages(session_id: str, order: list) -> int:
	"""Put the pages in a new order. `order` lists every current page index
	exactly once; the page at `order[0]` becomes the first page."""
	with _sessions_lock:
		session = _editable(session_id)
		n = len(session.pages)
		if (not isinstance(order, list) or len(order) != n
				or any(isinstance(i, bool) or not isinstance(i, int) for i in order)
				or sorted(order) != list(range(n))):
			raise IndexError('order must list each page number once')
		session.pages = [session.pages[i] for i in order]
		session.last_activity = datetime.now()
		return n


def rotate_page(session_id: str, page_index: int, degrees: int) -> None:
	"""Turn a page clockwise by 90, 180 or 270 degrees (or counter-clockwise
	with a negative number)."""
	if isinstance(degrees, bool) or not isinstance(degrees, int) or degrees % 90 or degrees % 360 == 0:
		raise ValueError('degrees must be a multiple of 90')
	with _sessions_lock:
		session = _editable(session_id)
		index = _check_index(session, page_index)
		data = session.pages[index]
	writer = PdfWriter()
	for page in PdfReader(BytesIO(data)).pages:
		page.rotate(degrees % 360)
		writer.add_page(page)
	out = BytesIO()
	writer.write(out)
	with _sessions_lock:
		session = _editable(session_id)
		# The list may have changed while we worked; find the page again.
		if index < len(session.pages) and session.pages[index] is data:
			session.pages[index] = out.getvalue()
		elif data in session.pages:
			session.pages[session.pages.index(data)] = out.getvalue()
		else:
			raise IndexError('That page changed. Try again.')
		session.last_activity = datetime.now()


def delete_page(session_id: str, page_index: int) -> int:
	"""Remove one page. Returns the new page count."""
	with _sessions_lock:
		session = _editable(session_id)
		del session.pages[_check_index(session, page_index)]
		session.last_activity = datetime.now()
		return len(session.pages)


def _page_from_image(data: bytes) -> bytes:
	try:
		img = Image.open(BytesIO(data))
		img.load()
	except Exception as e:
		raise UploadError('This photo cannot be read. Use a JPEG or PNG photo. '
		                  'On an iPhone, set the camera format to Most Compatible.') from e
	if img.format not in ('JPEG', 'PNG', 'MPO', 'HEIF', 'HEIC'):
		raise UploadError('Only JPEG, PNG, HEIC and PDF files can be added.')
	img = ImageOps.exif_transpose(img)
	if img.mode not in ('L', 'RGB'):
		img = img.convert('RGB')
	# Choose a resolution that makes the photo about letter or A4 size.
	dpi = max(72, round(max(img.size) / 11))
	out = BytesIO()
	img.save(out, format='PDF', resolution=dpi)
	return out.getvalue()


def _pages_from_pdf(data: bytes) -> list[bytes]:
	try:
		reader = PdfReader(BytesIO(data))
		if reader.is_encrypted:
			raise UploadError('This PDF is locked with a password.')
		parts = []
		for page in reader.pages:
			writer = PdfWriter()
			writer.add_page(page)
			out = BytesIO()
			writer.write(out)
			parts.append(out.getvalue())
	except UploadError:
		raise
	except Exception as e:
		raise UploadError('This PDF cannot be read.') from e
	if not parts:
		raise UploadError('This PDF has no pages.')
	return parts


def add_upload(session_id: str, data: bytes, content_type: str = '') -> int:
	"""Add an uploaded photo or PDF. Each PDF page becomes one page.
	Returns the new page count. Raises UploadError for a bad file."""
	if not data:
		raise UploadError('The file is empty.')
	if len(data) > MAX_UPLOAD_BYTES:
		raise UploadError('The file is larger than 25 MB.')
	kind = (content_type or '').split(';', 1)[0].strip().lower()
	if kind and not (kind.startswith('image/') or kind == 'application/pdf'
	                 or kind == 'application/octet-stream'):
		raise UploadError('Only JPEG, PNG, HEIC and PDF files can be added.')
	new_pages = _pages_from_pdf(data) if data.startswith(b'%PDF-') else [_page_from_image(data)]
	with _sessions_lock:
		session = _editable(session_id)
		if len(session.pages) + len(new_pages) > MAX_PAGES:
			raise UploadError(f'A document can have at most {MAX_PAGES} pages.')
		session.pages.extend(new_pages)
		session.last_activity = datetime.now()
		count = len(session.pages)
	logger.info('Added %d uploaded page(s) to session %s, now %d pages',
	            len(new_pages), session_id, count)
	return count
