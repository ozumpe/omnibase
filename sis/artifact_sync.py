"""sis.artifact_sync — the run's dataset reaches the artifacts bucket without a human (OMNI-140).

The episodic log is what the system learns from, and until this module it left
a box only when someone ran ``aws s3 sync`` by hand. A box replaced before that
took its log with it, twice: the third AWS run's, and the first cycle of the
fifth. Now ``loop.serve`` syncs every ``loop.artifact_sync_every`` cycles and
when it stops for any reason, so a killed process or a replaced box loses at
most that many cycles.

Deciding what to sync and when is pure (:func:`files_to_sync`, :func:`due`,
:func:`run_prefix`, :func:`manifest`); the upload sits behind an
:class:`Uploader`, so tests use a fake and never reach AWS.

**A run's folder holds the box's files whole.** The log, the audit and the
console log are appended to by every process on a box, and the CEO's spend
carries over by design, so ``runs/<start>/`` also holds every earlier run's
records. Each sync therefore uploads ``run.json`` last: where this process's
part of each appended file starts, its first and last ``cycle_id``, its
contracts and cost, and the carried spend at its start and now. A reader slices
the cumulative files with it instead of reading a folder as one run.

**A sync never breaks the loop.** It returns its failure instead of raising,
like a failed page: the dataset is what is at risk, and stopping the loop over
a slow bucket would put the next cycle's record at risk too.
"""

from __future__ import annotations

import datetime
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sis.atomic import write_text_atomic
from sis.paths import RUNTIME_DIR

# What a run leaves that cannot be re-created: the log, its latest-wins state
# (brakes, the pending PR, the code version), the operator audit and the
# console output the runbook tees to ``runtime/loop.log`` (OMNI-139). Text
# files only. A DuckDB file (``episodic.duckdb``) is a live database that may
# be mid-write, so a copy of it could not be trusted; the box uses ``jsonl``.
SYNCED_GLOBS = ("episodic*.jsonl", "episodic_state.json", "operator_audit*.jsonl",
                "loop*.log")

# The synced files every process appends to, so a run owns only their tail.
# Not ``episodic_state.json``: it is latest-wins, read whole.
APPENDED_GLOBS = ("episodic*.jsonl", "operator_audit*.jsonl", "loop*.log")
# The log the manifest reads this run's records from.
LOG_NAME = "episodic.jsonl"
STATE_NAME = "episodic_state.json"
MANIFEST_NAME = "run.json"

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


@dataclass(frozen=True)
class RunStart:
    """What the box held when this process started: where its own records begin."""

    started_at: datetime.datetime
    # Size in bytes of each appended file then; a file absent then is all this run's.
    sizes: dict[str, int]
    spent_usd: float | None


def manifest(start: RunStart, *, prefix: str, synced_at: datetime.datetime,
             sizes: dict[str, int], log_tail: bytes, state: dict[str, Any] | None,
             ) -> dict[str, Any]:
    """``run.json``: which part of the cumulative files is this run's. Pure.

    *sizes* are the appended files' sizes now, *log_tail* the episodic log from
    this run's first byte, *state* the episodic state now (None if unreadable).
    Counts and ids only, never a reason or a value: it says no more than the
    log beside it.
    """
    files = {name: {"from_byte": _from_byte(start.sizes.get(name, 0), size)}
             for name, size in sorted(sizes.items())}
    records = _records(log_tail)
    ids = [str(r["cycle_id"]) for r in records if r.get("cycle_id")]
    code = (state or {}).get("code_version")
    return {
        "started_at": start.started_at.isoformat(),
        "synced_at": synced_at.isoformat(),
        "prefix": prefix,
        "code_version": code if isinstance(code, dict) else None,
        "files": files,
        "records": len(records),
        "first_cycle_id": ids[0] if ids else None,
        "last_cycle_id": ids[-1] if ids else None,
        "contracts": sorted({str(r["contract"]) for r in records if r.get("contract")}),
        "cost_usd": round(sum(_number(r.get("cost_usd")) or 0.0 for r in records), 6),
        "spent_usd_at_start": start.spent_usd,
        "spent_usd": _spent(state),
    }


def _from_byte(at_start: int, now: int) -> int:
    # Shorter than at the start: replaced or cleared since, so all of it is new.
    # An offset past the end would hide this run's records instead.
    return at_start if at_start <= now else 0


def _records(tail: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in tail.decode("utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue   # a malformed line is skipped, as the store itself does
        if isinstance(record, dict):
            out.append(record)
    return out


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _spent(state: dict[str, Any] | None) -> float | None:
    ceo = (state or {}).get("ceo")
    return _number(ceo.get("spent_usd")) if isinstance(ceo, dict) else None


def _appended(runtime_dir: Path) -> dict[str, int]:
    """The appended files' sizes now. Pure but for the listing."""
    found = {p for pattern in APPENDED_GLOBS for p in runtime_dir.glob(pattern) if p.is_file()}
    return {p.name: p.stat().st_size for p in found}


def _read_state(runtime_dir: Path) -> dict[str, Any] | None:
    try:
        state = json.loads((runtime_dir / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def mark_start(runtime_dir: Path, started_at: datetime.datetime) -> RunStart:
    """What *runtime_dir* holds now, before this process's first cycle."""
    try:
        sizes = _appended(runtime_dir)
    except OSError:
        sizes = {}
    return RunStart(started_at, sizes, _spent(_read_state(runtime_dir)))


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
                 runtime_dir: Path = RUNTIME_DIR,
                 started_at: datetime.datetime | None = None) -> None:
        self._uploader = uploader
        self._runtime_dir = runtime_dir
        self.prefix = prefix
        self.bucket = bucket
        # Built before the first cycle (loop.serve does), so whatever the box
        # already holds belongs to earlier runs.
        self._start = mark_start(runtime_dir,
                                 started_at or datetime.datetime.now(datetime.UTC))

    @property
    def destination(self) -> str:
        return f"s3://{self.bucket}/{self.prefix}"

    def sync(self) -> SyncResult:
        """Upload every file that exists now. Never raises.

        One file failing does not stop the others: the log matters more than
        the audit, but neither should wait on the other. The first failure is
        reported. ``run.json`` goes last, written to *runtime_dir* first, and
        only when there is something for it to describe.
        """
        result = SyncResult()
        try:
            files = files_to_sync(self._runtime_dir)
        except OSError as exc:
            return SyncResult(error=f"cannot list {self._runtime_dir}: {_short(exc)}")
        if files:
            try:
                files.append(self._write_manifest())
            except Exception as exc:  # noqa: BLE001 - the files themselves still go
                result.error = f"{MANIFEST_NAME}: {_short(exc)}"
        for path in files:
            try:
                self._uploader.upload(path, f"{self.prefix}{path.name}")
                result.uploaded.append(path.name)
            except Exception as exc:  # noqa: BLE001 - a sync must never break the loop
                if result.error is None:
                    result.error = f"{path.name}: {_short(exc)}"
        return result

    def _write_manifest(self) -> Path:
        sizes = _appended(self._runtime_dir)
        log = self._runtime_dir / LOG_NAME
        tail = b""
        if LOG_NAME in sizes:
            with log.open("rb") as fh:
                fh.seek(_from_byte(self._start.sizes.get(LOG_NAME, 0), sizes[LOG_NAME]))
                # Up to the size measured above, so the record count matches it.
                tail = fh.read(max(0, sizes[LOG_NAME] - fh.tell()))
        body = manifest(self._start, prefix=self.prefix,
                        synced_at=datetime.datetime.now(datetime.UTC), sizes=sizes,
                        log_tail=tail, state=_read_state(self._runtime_dir))
        path = self._runtime_dir / MANIFEST_NAME
        write_text_atomic(path, json.dumps(body, indent=2) + "\n")
        return path


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
    return ArtifactSync(uploader, bucket=bucket, prefix=run_prefix(stamp), started_at=stamp)
