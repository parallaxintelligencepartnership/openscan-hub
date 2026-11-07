"""The "next feed" choice: which profile the feeder monitor uses next.

A user picks a profile (on the phone page or the dashboard) and then puts
paper in the feeder. The monitor uses that profile for one batch. The
choice clears after the batch is delivered, or after `TTL_SECONDS`. It is
kept in memory only, so a restart clears it.
"""

import threading
import time
from typing import Optional

TTL_SECONDS = 600

_lock = threading.Lock()
_profile_id: Optional[str] = None
_set_at = 0.0
_serial = 0

# Tests replace this to control time.
_now = time.monotonic


def set_next(profile_id: Optional[str]) -> None:
    """Set the next feed profile, or clear it when `profile_id` is empty."""
    global _profile_id, _set_at, _serial
    with _lock:
        _serial += 1
        _profile_id = profile_id or None
        _set_at = _now()


def clear() -> None:
    set_next(None)


def peek() -> Optional[tuple]:
    """(profile_id, expires_in_seconds, serial) or None when nothing is set."""
    global _profile_id
    with _lock:
        if _profile_id is None:
            return None
        left = TTL_SECONDS - (_now() - _set_at)
        if left <= 0:
            _profile_id = None
            return None
        return _profile_id, int(left + 0.999), _serial


def next_profile_id() -> Optional[str]:
    found = peek()
    return found[0] if found else None


def clear_if(serial: int) -> None:
    """Clear the choice only if nobody changed it since `peek` gave `serial`."""
    global _profile_id
    with _lock:
        if _serial == serial:
            _profile_id = None
