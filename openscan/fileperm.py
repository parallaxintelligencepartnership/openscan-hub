"""Give files written through tempfile.mkstemp the mode a normal write gets.

mkstemp creates files with mode 0600 and os.replace keeps it, so a scan saved
that way could not be read by Paperless or by other users of a share. The
umask is read once at import time, while the program is still single
threaded, because reading it means briefly changing it.
"""

from __future__ import annotations

import os

_UMASK = os.umask(0)
os.umask(_UMASK)


def apply_default_mode(fd: int) -> None:
    """Best effort: set 0666 minus the umask on an open file descriptor.

    Network shares (CIFS, NTFS mounts) may refuse; their own rules apply then.
    """
    fchmod = getattr(os, "fchmod", None)
    if fchmod is None:
        return
    try:
        fchmod(fd, 0o666 & ~_UMASK)
    except OSError:
        pass
