"""Multi-page scanning session management.

A session is not a scan: it collects pages the user scans one at a time.
Each page scan takes the global scanner lock for its own duration only
(`scan_page`), so the ADF monitor cannot interleave with it. While any
session has recent activity the monitor does not start auto-scans.
"""

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Optional

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


_sessions: dict[str, MultiPageSession] = {}
_sessions_lock = threading.RLock()


def ensure_pdf(data: bytes, resolution: int = 300) -> bytes:
	"""Convert image data to PDF if needed. Returns PDF bytes."""
	return pages.ensure_pdf(data, resolution)


def create_session(source: ScanSource) -> str:
	"""Create a new multi-page session."""
	session_id = str(uuid.uuid4())
	with _sessions_lock:
		_purge_locked()
		while len(_sessions) >= MAX_SESSIONS:
			oldest = min(_sessions.values(), key=lambda s: s.last_activity)
			del _sessions[oldest.session_id]
			logger.info('Evicted multipage session %s (too many sessions)', oldest.session_id)
		_sessions[session_id] = MultiPageSession(session_id=session_id, source=source)
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
			'created_at': session.created_at.isoformat(),
		}
