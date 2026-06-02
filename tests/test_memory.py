"""Tests for the episodic memory module."""

import pathlib
import tempfile

from sis import memory


def test_append_and_read(monkeypatch: object) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        log = pathlib.Path(tmpdir) / "test_log.jsonl"
        monkeypatch.setattr(memory, "LOG_PATH", log)  # type: ignore[attr-defined]

        assert memory.read_all() == []

        memory.append({"status": "promoted", "latency": 0.001})
        memory.append({"status": "rolled_back", "reason": "mypy failed"})

        records = memory.read_all()
        assert len(records) == 2
        assert records[0]["status"] == "promoted"
        assert records[1]["reason"] == "mypy failed"
