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

The new file keeps the old one's permissions, and a file that did not exist
gets what a plain write would give it (``0o666`` less the umask). None of the
callers writes a secret, so none asks for less.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
from pathlib import Path

_NAME_ATTEMPTS = 8


def write_text_atomic(path: Path, text: str) -> None:
    """Replace *path*'s contents with *text*, all at once or not at all.

    The temporary file lives in *path*'s own directory because ``os.replace``
    is only atomic within one filesystem. It is flushed and fsynced before the
    rename, and the directory after it, so the new contents survive a power
    loss rather than just a crash. On any failure the temporary file is
    removed and *path* is left exactly as it was.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = _create_temporary(path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            _keep_mode(fh.fileno(), path)
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        raise
    _fsync_directory(path.parent)


def _create_temporary(path: Path) -> tuple[int, Path]:
    """A new file beside *path*, created ``0o666`` less the umask.

    Not ``tempfile.mkstemp``: that creates its file 0600, and the replace then
    handed that mode to *path*, so ``config.yml`` became unreadable to any
    other user after the first save (OMNI-166).
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0)
    for _ in range(_NAME_ATTEMPTS):
        tmp = path.parent / f".{path.name}.{secrets.token_hex(8)}.tmp"
        try:
            return os.open(tmp, flags, 0o666), tmp
        except FileExistsError:
            continue
    raise FileExistsError(f"no free temporary name beside {path}")


def _keep_mode(fd: int, path: Path) -> None:
    # A rewrite keeps the permissions someone gave the file; a new file keeps
    # the umask-derived mode it was created with.
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        return
    os.fchmod(fd, mode)


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
