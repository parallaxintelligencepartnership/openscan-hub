"""Multi-page scanning session management."""

import logging
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from io import BytesIO
from typing import Optional

from pypdf import PdfReader, PdfWriter
from PIL import Image

from .scanner.base import ScanSource

logger = logging.getLogger(__name__)

# Session timeout
SESSION_MAX_AGE = timedelta(minutes=30)


@dataclass
class MultiPageSession:
	session_id: str
	pages: list[bytes] = field(default_factory=list)
	created_at: datetime = field(default_factory=datetime.now)
	source: ScanSource = ScanSource.FLATBED


_sessions: dict[str, MultiPageSession] = {}
_sessions_lock = threading.Lock()


def create_session(source: ScanSource) -> str:
	"""Create a new multi-page session."""
	cleanup_stale_sessions()
	session_id = str(uuid.uuid4())
	session = MultiPageSession(session_id=session_id, source=source)
	with _sessions_lock:
		_sessions[session_id] = session
	logger.info(f'Created multipage session {session_id} for {source.value}')
	return session_id


def get_session(session_id: str) -> Optional[MultiPageSession]:
	"""Get a session by ID."""
	with _sessions_lock:
		return _sessions.get(session_id)


def ensure_pdf(data: bytes, resolution: int = 300) -> bytes:
	"""Convert image data to PDF if needed. Returns PDF bytes."""
	if data.startswith(b'%PDF-'):
		return data

	# Try to convert image to PDF
	logger.info(f'Data is not PDF, attempting image conversion. Header: {data[:20]!r}')
	try:
		img = Image.open(BytesIO(data))
		if img.mode in ('RGBA', 'LA', 'P'):
			img = img.convert('RGB')
		output = BytesIO()
		img.save(output, format='PDF', resolution=resolution)
		pdf_bytes = output.getvalue()
		logger.info(f'Converted image to PDF: {len(pdf_bytes)} bytes')
		return pdf_bytes
	except Exception as e:
		logger.error(f'Failed to convert image to PDF: {e}')
		raise ValueError(f'Data is not a valid PDF or image: {data[:20]!r}')


def add_page(session_id: str, scan_data: bytes) -> int:
	"""Add a page to the session. Returns new page count."""
	session = get_session(session_id)
	if not session:
		raise ValueError(f'Session not found: {session_id}')

	logger.info(f'Adding page: {len(scan_data)} bytes, header: {scan_data[:20]!r}')

	# Convert to PDF if necessary
	pdf_bytes = ensure_pdf(scan_data)

	session.pages.append(pdf_bytes)
	logger.info(f'Added page to session {session_id}, now has {len(session.pages)} pages')
	return len(session.pages)


def merge_pages(session_id: str) -> bytes:
	"""Merge all pages in session into a single PDF."""
	session = get_session(session_id)
	if not session:
		raise ValueError(f'Session not found: {session_id}')
	if not session.pages:
		raise ValueError('No pages to merge')

	writer = PdfWriter()
	for i, pdf_bytes in enumerate(session.pages):
		logger.info(f'Merging page {i}: {len(pdf_bytes)} bytes, header: {pdf_bytes[:20]!r}')
		reader = PdfReader(BytesIO(pdf_bytes))
		for page in reader.pages:
			writer.add_page(page)

	output = BytesIO()
	writer.write(output)
	return output.getvalue()


def delete_session(session_id: str) -> None:
	"""Delete a session."""
	with _sessions_lock:
		if session_id in _sessions:
			del _sessions[session_id]
			logger.info(f'Deleted multipage session {session_id}')


def cleanup_stale_sessions() -> int:
	"""Remove sessions older than max age. Returns count removed."""
	now = datetime.now()
	stale_ids = []
	with _sessions_lock:
		for session_id, session in _sessions.items():
			if now - session.created_at > SESSION_MAX_AGE:
				stale_ids.append(session_id)
		for session_id in stale_ids:
			del _sessions[session_id]
	if stale_ids:
		logger.info(f'Cleaned up {len(stale_ids)} stale multipage sessions')
	return len(stale_ids)


def generate_thumbnail(pdf_bytes: bytes, width: int = 120) -> bytes:
	"""Convert first page of PDF to JPEG thumbnail."""
	import fitz  # PyMuPDF

	doc = fitz.open(stream=pdf_bytes, filetype="pdf")
	if doc.page_count == 0:
		raise ValueError('PDF has no pages')

	page = doc[0]
	# Calculate zoom to achieve target width
	zoom = width / page.rect.width
	mat = fitz.Matrix(zoom, zoom)
	pix = page.get_pixmap(matrix=mat)

	output = BytesIO()
	output.write(pix.tobytes("jpeg"))
	doc.close()
	return output.getvalue()


def get_page_thumbnail(session_id: str, page_index: int) -> bytes:
	"""Get JPEG thumbnail for a specific page in session."""
	session = get_session(session_id)
	if not session:
		raise ValueError(f'Session not found: {session_id}')
	if page_index < 0 or page_index >= len(session.pages):
		raise IndexError(f'Page index out of range: {page_index}')
	return generate_thumbnail(session.pages[page_index])


def get_session_info(session_id: str) -> Optional[dict]:
	"""Get session info as dict."""
	session = get_session(session_id)
	if not session:
		return None
	return {
		'session_id': session.session_id,
		'page_count': len(session.pages),
		'source': session.source.value,
		'created_at': session.created_at.isoformat(),
	}
