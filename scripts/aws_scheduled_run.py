"""scripts/aws_scheduled_run.py — one AWS test run, started and stopped by a task.

OMNI-168 (docs/AWS_RUN.md, "Scheduled runs"). The run box of OMNI-29 is
driven through AWS APIs only, so a scheduled task needs neither SSH nor the
SSM plugin: start the stopped instance, start the loop on it in tmux with
``ssm send-command``, watch ``runtime/loop.log``, stop the loop when it holds
for a PR, stops by itself or runs past a wall-clock limit, wait for its S3
sync, and stop the instance. The instance is stopped on every way out.

The caller never handles the run's credentials: the box reads the Anthropic
key from Secrets Manager with its instance role, inside the tmux session, as
in step 5a. Nothing here prints a secret, and none reaches SSM's output.

A box that is already running is refused, never touched: a human may be
using it. Merging the loop's PR, ``tofu apply`` and ``tofu destroy`` stay
human.

Usage:
    python scripts/aws_scheduled_run.py run --contract roman
    python scripts/aws_scheduled_run.py run --contract sum_of_divisors --dry-run
    python scripts/aws_scheduled_run.py status

Decisions (what the log says, what to do next) are pure functions; the AWS
calls are in :class:`Box`.
"""

from __future__ import annotations

import argparse
import dataclasses
import enum
import re
import sys
import time
from collections.abc import Callable, Sequence
from typing import Any

DEFAULT_REGION = "us-east-1"
DEFAULT_NAME = "sis-first-run"  # infra/aws variables.tf name_prefix, the instance's Name tag
DEFAULT_SECRET_ID = "sis/first-run/credentials"  # infra/aws variables.tf secret_name
TMUX_SESSION = "sis"
RUNNER_PATH = "/home/ubuntu/sis-scheduled-run.sh"
LOG_PATH = "/home/ubuntu/omnibase/runtime/loop.log"

# Every value that reaches a shell line is checked against one of these first.
_CONTRACT = re.compile(r"^[a-z_][a-z0-9_]{0,63}$")
_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")
_SECRET_ID = re.compile(r"^[A-Za-z0-9/_+=.@-]{1,512}$")

# SSM keeps 24,000 characters of a command's output; stay under it.
_TAIL_BYTES = 20_000


@dataclasses.dataclass(frozen=True)
class RunConfig:
    """What one run does, all of it checked before anything starts."""

    contract: str
    run_id: str
    max_cycles: int = 10
    budget_usd: float = 1.00
    breaker_threshold: int | None = None
    secret_id: str = DEFAULT_SECRET_ID

    def __post_init__(self) -> None:
        if not _CONTRACT.match(self.contract):
            raise ValueError(f"contract {self.contract!r} is not a contract name")
        if not _RUN_ID.match(self.run_id):
            raise ValueError(f"run id {self.run_id!r} is not YYYYMMDDTHHMMSSZ")
        if not _SECRET_ID.match(self.secret_id):
            raise ValueError(f"secret id {self.secret_id!r} is not a Secrets Manager name")
        if not 1 <= self.max_cycles <= 100:
            raise ValueError("max_cycles must be between 1 and 100")
        if not 0 < self.budget_usd <= 10:
            raise ValueError("budget_usd must be above 0 and at most 10")
        if self.breaker_threshold is not None and not 1 <= self.breaker_threshold <= 20:
            raise ValueError("breaker_threshold must be between 1 and 20")


def run_id_now(now: float | None = None) -> str:
    """A run id from the UTC time, the marker that finds this run in the log."""
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))


def start_marker(run_id: str) -> str:
    return f"[scheduled] {run_id} start"


def exit_marker(run_id: str) -> str:
    return f"[scheduled] {run_id} exit"


# --------------------------------------------------------------------------
# Box-side shell (pure): the box runs a release tag, so nothing here may rely
# on a script that is newer than the box. Each is sent inline.
# --------------------------------------------------------------------------

def runner_script(cfg: RunConfig) -> str:
    """The script tmux runs as ``ubuntu``: AWS_RUN.md steps 5a to 5c.

    The Anthropic key is read inside it, so it is never on disk and never in
    SSM's output. ``check_connections.py --deep`` failing ends the run before
    any spend.
    """
    flags = f"--contract {cfg.contract}"
    if cfg.breaker_threshold is not None:
        flags += f" --brakes-breaker-threshold {cfg.breaker_threshold}"
    flags += f" --loop --loop-max-cycles {cfg.max_cycles}"
    start, end = start_marker(cfg.run_id), exit_marker(cfg.run_id)
    return f"""#!/bin/bash
# Written by scripts/aws_scheduled_run.py (OMNI-168) for run {cfg.run_id}. No secrets.
set -o pipefail
cd ~/omnibase || exit 1
mkdir -p runtime
log=runtime/loop.log
echo "{start} $(date -u +%FT%TZ) $(git describe --tags --always --dirty)" | tee -a "$log"
export SIS_ENV=aws SIS_ADAPTERS=real SIS_AWS_SECRET_ID={cfg.secret_id}
export SIS_SANDBOX=docker SIS_PROPOSER=claude
export SIS_BUDGET_USD={cfg.budget_usd:.2f}
ANTHROPIC_API_KEY=$(aws secretsmanager get-secret-value --secret-id {cfg.secret_id} \\
  --query SecretString --output text | jq -r .anthropic.api_key)
export ANTHROPIC_API_KEY
if [ -z "$ANTHROPIC_API_KEY" ] || [ "$ANTHROPIC_API_KEY" = null ]; then
  echo "[scheduled] no Anthropic key in the secret" | tee -a "$log"
  echo "{end} 1" | tee -a "$log"; exit 1
fi
if ! poetry run python scripts/check_connections.py --deep 2>&1 | tee -a "$log"; then
  echo "[scheduled] check_connections failed; nothing was spent" | tee -a "$log"
  echo "{end} 1" | tee -a "$log"; exit 1
fi
poetry run python -u main.py {flags} 2>&1 | tee -i -a "$log"
code=$?
echo "{end} $code" | tee -a "$log"
"""


def start_commands(cfg: RunConfig) -> list[str]:
    """``AWS-RunShellScript`` lines (run as root) that start the loop in tmux.

    Refuses, with ``SIS_REFUSED``, if a loop or the tmux session already
    exists: two loops would not share the spend cap (M28).
    """
    script = runner_script(cfg)
    return [
        "set -eu",
        "if pgrep -f 'python.*main.py' >/dev/null; then "
        "echo 'SIS_REFUSED: a loop is already running'; pgrep -af 'main.py'; exit 3; fi",
        f"if sudo -iu ubuntu tmux has-session -t {TMUX_SESSION} 2>/dev/null; then "
        f"echo 'SIS_REFUSED: tmux session {TMUX_SESSION} exists'; exit 3; fi",
        f"cat > {RUNNER_PATH} <<'SIS_RUNNER_EOF'\n{script}SIS_RUNNER_EOF",
        f"chown ubuntu:ubuntu {RUNNER_PATH} && chmod 700 {RUNNER_PATH}",
        f"sudo -iu ubuntu tmux new-session -d -s {TMUX_SESSION} 'bash -l {RUNNER_PATH}'",
        f"echo 'SIS_STARTED {cfg.run_id}'",
    ]


def tail_commands(run_id: str) -> list[str]:
    """Print this run's part of the log: everything after its start marker."""
    if not _RUN_ID.match(run_id):
        raise ValueError(f"run id {run_id!r} is not YYYYMMDDTHHMMSSZ")
    # index(), not a regex: the marker's brackets would need escaping.
    return [
        f"test -f {LOG_PATH} || exit 0",
        f"awk -v m='{start_marker(run_id)}' 'index($0, m) == 1 {{p = 1}} p' {LOG_PATH}"
        f" | tail -c {_TAIL_BYTES}",
    ]


def interrupt_commands() -> list[str]:
    """Ctrl-C in the loop's pane: the graceful stop of step 6 (finish, sync)."""
    return [
        f"sudo -iu ubuntu tmux send-keys -t {TMUX_SESSION} q 2>/dev/null || true",
        f"sudo -iu ubuntu tmux send-keys -t {TMUX_SESSION} C-c 2>/dev/null || true",
    ]


# --------------------------------------------------------------------------
# Reading the log (pure)
# --------------------------------------------------------------------------

class Phase(enum.Enum):
    STARTING = "starting"   # no marker yet
    RUNNING = "running"
    HOLDING = "holding"     # waits for a human merge; stop it, nothing will change
    STOPPED = "stopped"     # the loop printed its stop line
    EXITED = "exited"       # the runner finished: nothing left to wait for


@dataclasses.dataclass(frozen=True)
class LogState:
    phase: Phase
    exit_code: int | None = None
    synced: bool = False
    crashed: bool = False


def read_log(text: str, run_id: str) -> LogState:
    """Where the run is, from its part of ``runtime/loop.log``."""
    if start_marker(run_id) not in text:
        return LogState(Phase.STARTING)
    synced = "[sis] artifacts synced to" in text
    crashed = "[loop] crashed:" in text
    end = re.search(re.escape(exit_marker(run_id)) + r" (-?\d+)", text)
    if end:
        return LogState(Phase.EXITED, int(end.group(1)), synced, crashed)
    if "[loop] stopped after" in text:
        return LogState(Phase.STOPPED, None, synced, crashed)
    if "[sis] HOLDING" in text:
        return LogState(Phase.HOLDING, None, synced, crashed)
    return LogState(Phase.RUNNING, None, synced, crashed)


class Action(enum.Enum):
    WAIT = "wait"
    INTERRUPT = "interrupt"
    DONE = "done"
    ABANDON = "abandon"     # the loop did not stop in time; stop the box anyway


def next_action(state: LogState, *, elapsed_s: float, limit_s: float,
                interrupted_at: float | None, grace_s: float) -> Action:
    """What the driver does next. Interrupt once; then give the loop its grace."""
    if state.phase is Phase.EXITED:
        return Action.DONE
    if interrupted_at is not None:
        return Action.ABANDON if elapsed_s - interrupted_at > grace_s else Action.WAIT
    if state.phase is Phase.HOLDING or elapsed_s >= limit_s:
        return Action.INTERRUPT
    return Action.WAIT


_SUMMARY = ("[sis] contract", "[sis] running", "[sis] HOLDING", "[sis] carrying on",
            "[cycle]", "[loop]", "[sis] artifacts", "[sis] WARNING", "[scheduled]")


def summary_lines(text: str, limit: int = 60) -> list[str]:
    """The lines worth reporting, in order, the last *limit* of them."""
    lines = [ln.rstrip() for ln in text.splitlines() if ln.startswith(_SUMMARY)]
    return lines[-limit:]


# --------------------------------------------------------------------------
# The box (AWS I/O)
# --------------------------------------------------------------------------

class Refused(RuntimeError):
    """The run did not start, and nothing was changed."""


class Box:
    """The run instance, through EC2 and SSM. *ec2* and *ssm* are boto3 clients."""

    def __init__(self, ec2: Any, ssm: Any, *, name: str = DEFAULT_NAME,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.ec2, self.ssm, self.name, self.sleep = ec2, ssm, name, sleep
        self._id: str | None = None

    @property
    def instance_id(self) -> str:
        if self._id is None:
            found = self._describe()
            if len(found) != 1:
                raise Refused(f"expected one instance tagged Name={self.name}, found {len(found)}")
            self._id = str(found[0]["InstanceId"])
        return self._id

    def _describe(self) -> list[dict[str, Any]]:
        resp = self.ec2.describe_instances(Filters=[
            {"Name": "tag:Name", "Values": [self.name]},
            {"Name": "instance-state-name",
             "Values": ["pending", "running", "stopping", "stopped"]},
        ])
        return [i for r in resp["Reservations"] for i in r["Instances"]]

    def state(self) -> str:
        resp = self.ec2.describe_instances(InstanceIds=[self.instance_id])
        return str(resp["Reservations"][0]["Instances"][0]["State"]["Name"])

    def start(self) -> None:
        self.ec2.start_instances(InstanceIds=[self.instance_id])
        self.ec2.get_waiter("instance_running").wait(InstanceIds=[self.instance_id])

    def stop(self) -> None:
        self.ec2.stop_instances(InstanceIds=[self.instance_id])
        self.ec2.get_waiter("instance_stopped").wait(InstanceIds=[self.instance_id])

    def wait_stopped(self) -> None:
        self.ec2.get_waiter("instance_stopped").wait(InstanceIds=[self.instance_id])

    def wait_online(self, timeout_s: float = 600, every_s: float = 10) -> None:
        waited = 0.0
        while waited < timeout_s:
            resp = self.ssm.describe_instance_information(
                Filters=[{"Key": "InstanceIds", "Values": [self.instance_id]}])
            info = resp.get("InstanceInformationList", [])
            if info and info[0].get("PingStatus") == "Online":
                return
            self.sleep(every_s)
            waited += every_s
        raise TimeoutError(f"{self.instance_id} not online in SSM after {timeout_s:.0f}s")

    def run(self, commands: Sequence[str], timeout_s: int = 300) -> tuple[str, str]:
        """Run *commands* as root; return (status, stdout)."""
        sent = self.ssm.send_command(
            InstanceIds=[self.instance_id], DocumentName="AWS-RunShellScript",
            Parameters={"commands": list(commands), "executionTimeout": [str(timeout_s)]},
            TimeoutSeconds=60, Comment="OMNI-168 scheduled run")
        command_id = sent["Command"]["CommandId"]
        for _ in range(timeout_s // 2 + 30):
            self.sleep(2)
            try:
                inv = self.ssm.get_command_invocation(
                    CommandId=command_id, InstanceId=self.instance_id)
            except self.ssm.exceptions.InvocationDoesNotExist:
                continue
            if inv["Status"] not in ("Pending", "InProgress", "Delayed"):
                return str(inv["Status"]), str(inv.get("StandardOutputContent", ""))
        raise TimeoutError(f"SSM command {command_id} did not finish")


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------

@dataclasses.dataclass
class Outcome:
    started: bool = False
    state: LogState = dataclasses.field(default_factory=lambda: LogState(Phase.STARTING))
    interrupted: bool = False
    abandoned: bool = False
    instance_stopped: bool = False
    lines: list[str] = dataclasses.field(default_factory=list)
    error: str | None = None


def run_once(box: Box, cfg: RunConfig, *, limit_s: float = 3600, grace_s: float = 900,
             poll_s: float = 60, clock: Callable[[], float] = time.monotonic,
             say: Callable[[str], None] = print) -> Outcome:
    """Start the box, run the loop, stop the loop, stop the box: always the last."""
    out = Outcome()
    state = box.state()
    if state != "stopped":
        raise Refused(f"{box.instance_id} is {state}; a scheduled run starts only a stopped box "
                      "(a human may be using it)")
    say(f"[scheduled] starting {box.instance_id} for run {cfg.run_id}")
    try:
        box.start()
        box.wait_online()
        status, stdout = box.run(start_commands(cfg))
        if "SIS_STARTED" not in stdout:
            out.error = f"the loop did not start ({status}): {stdout.strip()[-500:]}"
            return out
        out.started = True
        say(f"[scheduled] loop started: --contract {cfg.contract}")
        began = clock()
        interrupted_at: float | None = None
        text = ""
        while True:
            box.sleep(poll_s)
            _, text = box.run(tail_commands(cfg.run_id), timeout_s=60)
            out.state = read_log(text, cfg.run_id)
            elapsed = clock() - began
            action = next_action(out.state, elapsed_s=elapsed, limit_s=limit_s,
                                 interrupted_at=interrupted_at, grace_s=grace_s)
            if action is Action.DONE:
                break
            if action is Action.ABANDON:
                out.abandoned = True
                say("[scheduled] the loop did not stop in its grace period; stopping the box")
                break
            if action is Action.INTERRUPT:
                why = "it holds for a PR" if out.state.phase is Phase.HOLDING else "time limit"
                say(f"[scheduled] stopping the loop ({why})")
                box.run(interrupt_commands(), timeout_s=60)
                out.interrupted, interrupted_at = True, elapsed
        out.lines = summary_lines(text)
        return out
    except Exception as exc:  # noqa: BLE001 - reported, and the box is still stopped below
        out.error = f"{type(exc).__name__}: {exc}"
        return out
    finally:
        try:
            box.stop()
            out.instance_stopped = box.state() == "stopped"
        except Exception as exc:  # noqa: BLE001 - say so loudly: the box costs money
            out.error = (out.error or "") + f"; could not stop the instance: {exc}"
        say(f"[scheduled] instance {'stopped' if out.instance_stopped else 'NOT stopped'}")


def report(cfg: RunConfig, out: Outcome) -> str:
    """The text a task posts after a run."""
    head = [f"run {cfg.run_id}, contract {cfg.contract}: "
            f"{out.state.phase.value}, exit code {out.state.exit_code}",
            f"loop synced to S3: {'yes' if out.state.synced else 'NO'}; "
            f"instance stopped: {'yes' if out.instance_stopped else 'NO, stop it by hand'}"]
    if out.interrupted:
        head.append("the loop was interrupted (a hold or the time limit)")
    if out.abandoned:
        head.append("the loop did not stop within its grace period")
    if out.state.crashed:
        head.append("the loop CRASHED, see the log")
    if out.error:
        head.append(f"error: {out.error}")
    return "\n".join(head + [""] + out.lines)


def _clients(region: str) -> tuple[Any, Any]:
    import boto3

    return boto3.client("ec2", region_name=region), boto3.client("ssm", region_name=region)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--name", default=DEFAULT_NAME, help="the instance's Name tag")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="one run: start, loop, stop")
    run.add_argument("--contract", required=True)
    run.add_argument("--max-cycles", type=int, default=10)
    run.add_argument("--budget-usd", type=float, default=1.00)
    run.add_argument("--breaker-threshold", type=int, default=None)
    run.add_argument("--limit-minutes", type=float, default=60)
    run.add_argument("--dry-run", action="store_true",
                     help="print what would be sent to the box; call no AWS API")
    sub.add_parser("status", help="the instance's state")
    args = parser.parse_args(argv)

    if args.command == "run":
        cfg = RunConfig(contract=args.contract, run_id=run_id_now(),
                        max_cycles=args.max_cycles, budget_usd=args.budget_usd,
                        breaker_threshold=args.breaker_threshold)
        if args.dry_run:
            print("\n".join(start_commands(cfg)))
            return 0
        ec2, ssm = _clients(args.region)
        box = Box(ec2, ssm, name=args.name)
        try:
            out = run_once(box, cfg, limit_s=args.limit_minutes * 60)
        except Refused as exc:
            print(f"[scheduled] refused: {exc}", file=sys.stderr)
            return 3
        print(report(cfg, out))
        ok = out.started and out.instance_stopped and out.error is None
        return 0 if ok and out.state.phase is Phase.EXITED else 1

    ec2, ssm = _clients(args.region)
    box = Box(ec2, ssm, name=args.name)
    print(f"{box.instance_id} {box.state()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
