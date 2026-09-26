"""sis.atomic — replace a file's contents so a reader sees the old file or the new one.

A plain ``write_text`` truncates first and writes second, so a crash in between
leaves a half-written file. For the CEO's brake state that half-written file
used to read as "no state", and the spend cap and failure streak restarted from
zero without a word (OMNI-61). :func:`write_text_atomic` writes a temporary file
in the same directory, flushes it to disk, and ``os.replace``-s it over the
original — a rename is atomic on POSIX, so there is no moment at which the path
holds anything but a complete file.

Shared on purpose: OMNI-52 needs the same discipline for ``config.yml``, and two
hand-rolled copies of this are how one of them ends up without the fsync.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path


def write_text_atomic(path: Path, text: str) -> None:
    """Replace *path*'s contents with *text*, all at once or not at all.

    The temporary file lives in *path*'s own directory because ``os.replace``
    is only atomic within one filesystem. It is flushed and fsynced before the
    rename, and the directory after it, so the new contents survive a power
    loss rather than just a crash. On any failure the temporary file is
    removed and *path* is left exactly as it was.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    _fsync_directory(path.parent)


def _fsync_directory(directory: Path) -> None:
    # Makes the rename itself durable. Not every platform lets a directory be
    # opened for this; the replace has already happened, so that is not fatal.
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
