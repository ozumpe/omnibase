"""OMNI-140: the run's dataset reaches the artifacts bucket without a human. No Ray, no AWS."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

import pytest

from sis import artifact_sync
from sis.artifact_sync import ArtifactSync, S3Uploader


class _Recorder:
    """An Uploader that remembers what it was given."""

    def __init__(self, fail_on: str | None = None) -> None:
        self.calls: list[tuple[str, str, bytes]] = []
        self._fail_on = fail_on

    def upload(self, path: Path, key: str) -> None:
        if self._fail_on and path.name == self._fail_on:
            raise ConnectionError("Read timeout on endpoint URL")
        self.calls.append((path.name, key, path.read_bytes()))


def _runtime(tmp_path: Path) -> Path:
    runtime = tmp_path / "runtime"
    (runtime / "candidates").mkdir(parents=True)
    (runtime / "episodic.jsonl").write_text('{"cycle": 1}\n')
    (runtime / "episodic_state.json").write_text('{"ceo": {}}')
    (runtime / "operator_audit.jsonl").write_text('{"key": "x"}\n')
    (runtime / "loop.log").write_text("[loop] stopped after 3 cycle(s)\n")   # OMNI-139
    (runtime / "episodic.duckdb").write_bytes(b"a live database")
    (runtime / "target.py").write_text("def f(): ...\n")
    (runtime / "candidates" / "optimised_target.py").write_text("x = 1\n")
    return runtime


def test_a_run_prefix_is_the_layout_the_runbook_uses() -> None:
    stamp = datetime.datetime(2026, 9, 29, 20, 28, 51, tzinfo=datetime.UTC)
    assert artifact_sync.run_prefix(stamp) == "runs/20260929-2028/"


def test_only_the_dataset_is_synced(tmp_path: Path) -> None:
    # The log, its state and the audit. Not the target or the candidates (they
    # are code, and on GitHub), and not a DuckDB file that may be mid-write.
    names = [p.name for p in artifact_sync.files_to_sync(_runtime(tmp_path))]
    assert names == ["episodic.jsonl", "episodic_state.json", "loop.log",
                     "operator_audit.jsonl"]


def test_a_run_with_no_audit_yet_syncs_what_exists(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime / "operator_audit.jsonl").unlink()
    assert [p.name for p in artifact_sync.files_to_sync(runtime)] == [
        "episodic.jsonl", "episodic_state.json", "loop.log"]


def test_an_empty_or_missing_runtime_dir_syncs_nothing(tmp_path: Path) -> None:
    assert artifact_sync.files_to_sync(tmp_path / "nowhere") == []
    sync = ArtifactSync(_Recorder(), bucket="b", prefix="runs/x/", runtime_dir=tmp_path / "nowhere")
    assert sync.sync().uploaded == [] and sync.sync().error is None


@pytest.mark.parametrize(("every", "due_at"), [
    (1, [1, 2, 3, 4, 5, 6, 7, 8, 9]),
    (3, [3, 6, 9]),
    (0, []),        # only when the loop stops
    (-2, []),
])
def test_a_periodic_sync_follows_every_nth_cycle(every: int, due_at: list[int]) -> None:
    assert [n for n in range(0, 10) if artifact_sync.due(n, every)] == due_at


def test_every_sync_of_a_run_overwrites_the_same_keys(tmp_path: Path) -> None:
    # One run, one prefix: a run must not scatter across a directory per sync.
    runtime = _runtime(tmp_path)
    uploader = _Recorder()
    sync = ArtifactSync(uploader, bucket="b", prefix="runs/20260929-2028/", runtime_dir=runtime)

    first = sync.sync()
    (runtime / "episodic.jsonl").write_text('{"cycle": 1}\n{"cycle": 2}\n')
    second = sync.sync()

    assert first.uploaded == second.uploaded == [
        "episodic.jsonl", "episodic_state.json", "loop.log", "operator_audit.jsonl"]
    keys = [k for _, k, _ in uploader.calls]
    assert set(keys) == {f"runs/20260929-2028/{n}" for n in first.uploaded}
    assert len(keys) == 8 and len(set(keys)) == 4
    newer = b'{"cycle": 1}\n{"cycle": 2}\n'
    assert uploader.calls[4][2] == newer, "the second sync sends the newer log"
    assert sync.destination == "s3://b/runs/20260929-2028/"


def test_a_failing_upload_is_reported_never_raised(tmp_path: Path) -> None:
    sync = ArtifactSync(_Recorder(fail_on="episodic.jsonl"), bucket="b", prefix="runs/x/",
                        runtime_dir=_runtime(tmp_path))
    result = sync.sync()          # must not raise
    assert result.error is not None and "episodic.jsonl" in result.error
    assert "ConnectionError" in result.error and "Read timeout" in result.error
    # The other files still went: one bad file must not cost the rest.
    assert result.uploaded == ["episodic_state.json", "loop.log", "operator_audit.jsonl"]


def test_the_first_failure_is_the_one_reported(tmp_path: Path) -> None:
    class _AlwaysDown:
        def upload(self, path: Path, key: str) -> None:
            raise OSError(f"cannot send {path.name}")

    result = ArtifactSync(_AlwaysDown(), bucket="b", prefix="runs/x/",
                          runtime_dir=_runtime(tmp_path)).sync()
    assert result.uploaded == []
    assert result.error == "episodic.jsonl: OSError: cannot send episodic.jsonl"


# --- the S3 uploader ---


class _FakeS3:
    def __init__(self) -> None:
        self.puts: list[dict[str, Any]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.puts.append(kwargs)


def test_the_s3_uploader_puts_the_bytes_under_the_key(tmp_path: Path) -> None:
    client = _FakeS3()
    f = tmp_path / "episodic.jsonl"
    f.write_bytes(b'{"cycle": 1}\n')

    S3Uploader("sis-first-run-artifacts-1", "us-east-1", client=client).upload(
        f, "runs/20260929-2028/episodic.jsonl")

    assert client.puts == [{"Bucket": "sis-first-run-artifacts-1",
                            "Key": "runs/20260929-2028/episodic.jsonl",
                            "Body": b'{"cycle": 1}\n'}]


def test_the_real_client_has_timeouts_that_cannot_stall_a_cycle() -> None:
    assert artifact_sync.CONNECT_TIMEOUT_S <= 5
    assert artifact_sync.READ_TIMEOUT_S <= 30
    assert artifact_sync.MAX_ATTEMPTS <= 3


# --- from the configuration ---


def test_no_bucket_means_no_sync_and_no_noise(monkeypatch: pytest.MonkeyPatch,
                                              capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.delenv("SIS_ARTIFACTS_BUCKET", raising=False)
    assert artifact_sync.from_config() is None
    out, err = capsys.readouterr()
    assert (out, err) == ("", ""), "a run with no bucket must not talk about it"


def test_a_configured_bucket_builds_a_sync_for_this_process(
        monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[tuple[str, str]] = []

    class _Fake:
        def __init__(self, bucket: str, region: str) -> None:
            built.append((bucket, region))

        def upload(self, path: Path, key: str) -> None: ...

    monkeypatch.setenv("SIS_ARTIFACTS_BUCKET", "sis-first-run-artifacts-1")
    monkeypatch.setattr(artifact_sync, "S3Uploader", _Fake)
    now = datetime.datetime(2026, 9, 29, 20, 28, tzinfo=datetime.UTC)

    sync = artifact_sync.from_config(now)

    assert sync is not None
    assert sync.destination == "s3://sis-first-run-artifacts-1/runs/20260929-2028/"
    assert built == [("sis-first-run-artifacts-1", "us-east-1")]


def test_an_uploader_that_cannot_be_built_syncs_nothing_and_says_so(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _no_boto(bucket: str, region: str) -> Any:
        raise ModuleNotFoundError("No module named 'boto3'")

    monkeypatch.setenv("SIS_ARTIFACTS_BUCKET", "b")
    monkeypatch.setattr(artifact_sync, "S3Uploader", _no_boto)

    assert artifact_sync.from_config() is None
    err = capsys.readouterr().err
    assert "will not be synced to s3://b" in err and "boto3" in err
