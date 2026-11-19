"""OMNI-166 (L55): an atomic write keeps the file's permissions.

``tempfile.mkstemp`` creates its file 0600, and ``os.replace`` put that file in
place of the original. So the first operator save, or ``python -m sis.config
--write``, turned ``config.yml`` from 0644 into 0600, and a console or CI step
under another uid then failed with an unrelated-looking PermissionError.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Iterator
from pathlib import Path

import pytest

from sis.atomic import write_text_atomic

pytestmark = pytest.mark.skipif(os.name != "posix", reason="POSIX file modes")


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.fixture
def umask_022() -> Iterator[None]:
    old = os.umask(0o022)
    yield
    os.umask(old)


@pytest.mark.parametrize("mode", [0o644, 0o640, 0o600, 0o664])
def test_a_rewrite_keeps_the_files_mode(tmp_path: Path, umask_022: None, mode: int) -> None:
    target = tmp_path / "config.yml"
    target.write_text("old: 1\n", encoding="utf-8")
    target.chmod(mode)
    write_text_atomic(target, "new: 2\n")
    assert target.read_text(encoding="utf-8") == "new: 2\n"
    assert _mode(target) == mode


@pytest.mark.parametrize(("umask", "expected"), [(0o022, 0o644), (0o077, 0o600), (0o002, 0o664)])
def test_a_new_file_gets_the_mode_a_plain_write_would(
        tmp_path: Path, umask: int, expected: int) -> None:
    old = os.umask(umask)
    try:
        write_text_atomic(tmp_path / "state.json", "{}")
        plain = tmp_path / "plain.json"
        plain.write_text("{}", encoding="utf-8")
    finally:
        os.umask(old)
    assert _mode(tmp_path / "state.json") == expected == _mode(plain)


def test_a_failed_write_leaves_no_temporary_file_and_the_mode_alone(
        tmp_path: Path, umask_022: None, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "config.yml"
    target.write_text("old: 1\n", encoding="utf-8")
    target.chmod(0o640)

    def crash(*args: object) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr("sis.atomic.os.replace", crash)
    with pytest.raises(OSError, match="simulated crash"):
        write_text_atomic(target, "new: 2\n")
    assert target.read_text(encoding="utf-8") == "old: 1\n" and _mode(target) == 0o640
    assert [p.name for p in tmp_path.iterdir()] == ["config.yml"]


def test_two_writes_in_a_row_both_succeed(tmp_path: Path) -> None:
    # The temporary name is fresh each time, so a second write never meets
    # the first one's leftovers.
    target = tmp_path / "state.json"
    write_text_atomic(target, "1")
    write_text_atomic(target, "2")
    assert target.read_text(encoding="utf-8") == "2"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]
