"""sis.artifact_sync — the run's dataset reaches the artifacts bucket without a human (OMNI-140).

The episodic log is what the system learns from, and until this module it left
a box only when someone ran ``aws s3 sync`` by hand. A box replaced before that
took its log with it, twice: the third AWS run's, and the first cycle of the
fifth. Now ``loop.serve`` syncs every ``loop.artifact_sync_every`` cycles and
when it stops for any reason, so a killed process or a replaced box loses at
most that many cycles.

Deciding what to sync and when is pure (:func:`files_to_sync`, :func:`due`,
:func:`run_prefix`); the upload sits behind an :class:`Uploader`, so tests use a
fake and never reach AWS.

**A sync never breaks the loop.** It returns its failure instead of raising,
like a failed page: the dataset is what is at risk, and stopping the loop over
a slow bucket would put the next cycle's record at risk too.
"""

from __future__ import annotations

import datetime
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sis.paths import RUNTIME_DIR

# What a run leaves that cannot be re-created: the log, its latest-wins state
# (brakes, the pending PR, the code version), the operator audit and the
# console output the runbook tees to ``runtime/loop.log`` (OMNI-139). Text
# files only. A DuckDB file (``episodic.duckdb``) is a live database that may
# be mid-write, so a copy of it could not be trusted; the box uses ``jsonl``.
SYNCED_GLOBS = ("episodic*.jsonl", "episodic_state.json", "operator_audit*.jsonl",
                "loop*.log")

# A slow or unreachable bucket costs the loop a few seconds, not a stall.
CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 15
MAX_ATTEMPTS = 2


def run_prefix(now: datetime.datetime) -> str:
    """The key prefix for one process's syncs, e.g. ``runs/20260929-2028/``.

    The layout the runbook's manual sync has always used, so a human reading
    the bucket finds the same shape. Fixed at process start: every sync of one
    run overwrites the same keys, rather than scattering a run across prefixes.
    """
    return f"runs/{now:%Y%m%d-%H%M}/"


def files_to_sync(runtime_dir: Path) -> list[Path]:
    """The files under *runtime_dir* worth keeping, sorted. Pure but for the listing."""
    found = {p for pattern in SYNCED_GLOBS for p in runtime_dir.glob(pattern) if p.is_file()}
    return sorted(found)


def due(cycles_run: int, every: int) -> bool:
    """Whether a periodic sync follows the cycle that just finished. Pure.

    ``every <= 0`` means "only when the loop stops", which the caller does
    regardless of this.
    """
    return every > 0 and cycles_run > 0 and cycles_run % every == 0


class Uploader(Protocol):
    def upload(self, path: Path, key: str) -> None:
        """Store *path* under *key*. Raises on any failure."""
        ...


class S3Uploader:
    """Puts small files into one bucket, with timeouts short enough not to stall a cycle."""

    def __init__(self, bucket: str, region: str, *, client: Any = None) -> None:
        self._bucket = bucket
        if client is None:
            import boto3  # lazy: only a run that syncs needs it (the `real` group)
            from botocore.config import Config

            client = boto3.client("s3", region_name=region, config=Config(
                connect_timeout=CONNECT_TIMEOUT_S, read_timeout=READ_TIMEOUT_S,
                retries={"max_attempts": MAX_ATTEMPTS}))
        self._client = client

    def upload(self, path: Path, key: str) -> None:
        # put_object, not upload_file: these are small, and a plain request obeys
        # the timeouts above without spawning transfer threads.
        self._client.put_object(Bucket=self._bucket, Key=key, Body=path.read_bytes())


@dataclass
class SyncResult:
    uploaded: list[str] = field(default_factory=list)
    error: str | None = None


class ArtifactSync:
    """One process's syncs: where they go, and how to send them."""

    def __init__(self, uploader: Uploader, *, bucket: str, prefix: str,
                 runtime_dir: Path = RUNTIME_DIR) -> None:
        self._uploader = uploader
        self._runtime_dir = runtime_dir
        self.prefix = prefix
        self.bucket = bucket

    @property
    def destination(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def sync(self) -> SyncResult:
        """Upload every file that exists now. Never raises.

        One file failing does not stop the others: the log matters more than
        the audit, but neither should wait on the other. The first failure is
        reported.
        """
        result = SyncResult()
        try:
            files = files_to_sync(self._runtime_dir)
        except OSError as exc:
            return SyncResult(error=f"cannot list {self._runtime_dir}: {_short(exc)}")
        for path in files:
            try:
                self._uploader.upload(path, f"{self.prefix}{path.name}")
                result.uploaded.append(path.name)
            except Exception as exc:  # noqa: BLE001 - a sync must never break the loop
                if result.error is None:
                    result.error = f"{path.name}: {_short(exc)}"
        return result


def _short(exc: BaseException) -> str:
    return " ".join(f"{type(exc).__name__}: {exc}".split())[:200]


def from_config(now: datetime.datetime | None = None) -> ArtifactSync | None:
    """The sync this process is configured for, or None to sync nothing.

    None without ``adapters.artifacts_bucket``, and also when the uploader
    cannot be built (no boto3, say): it says so and carries on, since the run
    it would have protected is the one that matters.
    """
    from sis import config

    cfg = config.config()
    bucket = cfg.adapters.artifacts_bucket
    if not bucket:
        return None
    try:
        uploader = S3Uploader(bucket, cfg.adapters.aws_region)
    except Exception as exc:  # noqa: BLE001
        print(f"[sis] WARNING: artifacts will not be synced to s3://{bucket}: {_short(exc)}",
              file=sys.stderr)
        return None
    stamp = now or datetime.datetime.now(datetime.UTC)
    return ArtifactSync(uploader, bucket=bucket, prefix=run_prefix(stamp))
