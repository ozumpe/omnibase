"""Tests for scripts/aws_scheduled_run.py — a scheduled AWS run (OMNI-168, no AWS calls)."""

import importlib.util
import pathlib
import subprocess
import sys
from types import ModuleType
from typing import Any

import pytest

_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "aws_scheduled_run.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("aws_scheduled_run", _PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["aws_scheduled_run"] = module
    spec.loader.exec_module(module)
    return module


sr = _load()

RUN = "20261029T150000Z"


def _cfg(**kw: Any) -> Any:
    return sr.RunConfig(contract=kw.pop("contract", "roman"), run_id=RUN, **kw)


# --- configuration: nothing unchecked reaches a shell line --------------------

@pytest.mark.parametrize("contract", ["roman; rm -rf ~", "Roman", "", "a b", "$(id)", "x" * 80])
def test_a_contract_that_is_not_a_name_is_refused(contract: str) -> None:
    with pytest.raises(ValueError):
        _cfg(contract=contract)


@pytest.mark.parametrize("kw", [{"max_cycles": 0}, {"max_cycles": 101}, {"budget_usd": 0.0},
                                {"budget_usd": 50.0}, {"breaker_threshold": 0},
                                {"secret_id": "a'b"}])
def test_limits_out_of_range_are_refused(kw: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        _cfg(**kw)


def test_a_run_id_is_a_utc_timestamp() -> None:
    assert sr.run_id_now(0) == "19700101T000000Z"
    with pytest.raises(ValueError):
        sr.RunConfig(contract="roman", run_id="today")


# --- the box-side script ------------------------------------------------------

def test_the_runner_follows_step_5_with_the_docker_sandbox_and_the_cap() -> None:
    script = sr.runner_script(_cfg(breaker_threshold=5, max_cycles=7, budget_usd=0.5))
    assert "SIS_SANDBOX=docker SIS_PROPOSER=claude" in script
    assert "SIS_BUDGET_USD=0.50" in script
    assert ("main.py --contract roman --brakes-breaker-threshold 5 --loop --loop-max-cycles 7"
            in script)
    assert "tee -i -a" in script
    # check_connections comes before the loop, and its failure ends the run.
    assert script.index("check_connections.py --deep") < script.index("main.py --contract")
    # Its lines reach the report, so a failed preflight says which check failed.
    assert "s/^/[check] /" in script
    assert sr.summary_lines("  noise\n[check]   ✗ Pager  no confirmed subscription\n") == [
        "[check]   ✗ Pager  no confirmed subscription"]
    assert "SIS_ALLOW_UNSANDBOXED_LLM" not in script and "canary" not in script


def test_the_runner_reads_the_key_on_the_box_and_never_prints_it() -> None:
    script = sr.runner_script(_cfg())
    assert "jq -r .anthropic.api_key" in script
    assert "echo $ANTHROPIC_API_KEY" not in script and 'echo "$ANTHROPIC_API_KEY' not in script
    # A secret the box cannot read says why (the CLI's error carries no value),
    # rather than looking like a secret with no key in it.
    assert "could not read the secret" in script and "unset secret" in script


def test_the_start_refuses_a_second_loop_and_writes_a_valid_script(tmp_path: pathlib.Path) -> None:
    lines = sr.start_commands(_cfg())
    joined = "\n".join(lines)
    assert "SIS_REFUSED: a loop is already running" in joined
    assert "tmux has-session" in joined and "tmux new-session -d -s sis" in joined
    path = tmp_path / "start.sh"
    path.write_text(joined + "\n")
    assert subprocess.run(["bash", "-n", str(path)], check=False).returncode == 0


def test_the_tail_shows_only_this_runs_part_of_the_log(tmp_path: pathlib.Path) -> None:
    log = tmp_path / "loop.log"
    log.write_text("[scheduled] 20261022T150000Z start\n[loop] stopped after 3 cycle(s): old\n"
                   f"[scheduled] {RUN} start 2026-10-29T15:00:00Z v0.3.7\n[cycle] new\n")
    cmd = sr.tail_commands(RUN)[1].replace(sr.LOG_PATH, str(log))
    out = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, check=True).stdout
    assert out.startswith(f"[scheduled] {RUN} start") and "[cycle] new" in out
    assert "old" not in out


def _tail(tmp_path: pathlib.Path, text: str) -> str:
    log = tmp_path / "loop.log"
    log.write_text(text)
    cmd = sr.tail_commands(RUN)[1].replace(sr.LOG_PATH, str(log))
    return subprocess.run(["bash", "-c", cmd], capture_output=True, text=True,
                          check=True).stdout


def test_a_long_run_is_still_read_once_its_log_passes_the_tail(tmp_path: pathlib.Path) -> None:
    # M35 (OMNI-171): the tail alone dropped the start marker past 20 KB, so a
    # run that had exited read as STARTING and the driver waited out its
    # limit and grace (~75 minutes) before abandoning it.
    cycles = [f"[cycle] {i} rolled_back: benchmark " + "x" * 200 for i in range(400)]
    text = "\n".join(["[scheduled] 20261022T150000Z start", "[loop] crashed: old",
                      f"[scheduled] {RUN} start 2026-10-29T15:00:00Z v0.4.0",
                      "[sis] artifacts synced to s3://b/runs/1", *cycles,
                      "[loop] stopped after 400 cycle(s): converged",
                      "[sis] artifacts synced to s3://b/runs/2",
                      f"[scheduled] {RUN} exit 0"]) + "\n"
    assert len(text) > 4 * sr._TAIL_BYTES
    out = _tail(tmp_path, text)
    assert len(out) < 24_000              # what SSM keeps of a command's output
    state = sr.read_log(out, RUN)
    assert (state.phase, state.exit_code, state.synced, state.crashed) == (
        sr.Phase.EXITED, 0, True, False)
    assert "s3://b/runs/2" in out and "s3://b/runs/1" not in out   # the last of each kind
    lines = sr.summary_lines(sr.log_tail(out))
    assert lines[-1] == f"[scheduled] {RUN} exit 0" and lines.count(lines[-1]) == 1


def test_a_long_status_line_is_cut_and_the_output_stays_under_ssms_limit(
        tmp_path: pathlib.Path) -> None:
    text = "\n".join([f"[scheduled] {RUN} start", "[loop] crashed: " + "y" * 50_000,
                      *(["z" * 999] * 60)]) + "\n"
    out = _tail(tmp_path, text)
    assert len(out) < 24_000
    assert sr.read_log(out, RUN).crashed


# --- reading the log and deciding --------------------------------------------

def _log(*lines: str) -> str:
    return "\n".join([f"[scheduled] {RUN} start", *lines]) + "\n"


def test_read_log_phases() -> None:
    assert sr.read_log("", RUN).phase is sr.Phase.STARTING
    assert sr.read_log(_log("[sis] contract: roman"), RUN).phase is sr.Phase.RUNNING
    assert sr.read_log(_log("[sis] HOLDING: PR 23 is open"), RUN).phase is sr.Phase.HOLDING
    stopped = sr.read_log(_log("[loop] stopped after 3 cycle(s): x"), RUN)
    assert stopped.phase is sr.Phase.STOPPED
    done = sr.read_log(_log("[loop] crashed: KeyError: x", "[sis] artifacts synced to s3://b/r",
                            f"[scheduled] {RUN} exit 1"), RUN)
    assert (done.phase, done.exit_code, done.synced, done.crashed) == (sr.Phase.EXITED, 1,
                                                                       True, True)


def test_a_hold_is_interrupted_at_once_and_a_run_at_its_limit() -> None:
    holding = sr.LogState(sr.Phase.HOLDING)
    running = sr.LogState(sr.Phase.RUNNING)
    kw: dict[str, Any] = {"limit_s": 3600, "interrupted_at": None, "grace_s": 900}
    assert sr.next_action(holding, elapsed_s=60, **kw) is sr.Action.INTERRUPT
    assert sr.next_action(running, elapsed_s=60, **kw) is sr.Action.WAIT
    assert sr.next_action(running, elapsed_s=3600, **kw) is sr.Action.INTERRUPT
    kw["interrupted_at"] = 100.0
    assert sr.next_action(holding, elapsed_s=200, **kw) is sr.Action.WAIT  # never twice
    assert sr.next_action(holding, elapsed_s=1001, **kw) is sr.Action.ABANDON
    assert sr.next_action(sr.LogState(sr.Phase.EXITED, 0), elapsed_s=5, **kw) is sr.Action.DONE


def test_summary_keeps_the_lines_worth_reporting() -> None:
    text = _log("noise", "[cycle] rolled_back: acceptance", "[loop] stopped after 1 cycle(s)")
    assert sr.summary_lines(text) == [f"[scheduled] {RUN} start",
                                      "[cycle] rolled_back: acceptance",
                                      "[loop] stopped after 1 cycle(s)"]


# --- one run, against fakes ---------------------------------------------------

class _Waiter:
    def __init__(self, ec2: "_FakeEC2", state: str) -> None:
        self.ec2, self.target = ec2, state

    def wait(self, **_: Any) -> None:
        self.ec2.state = self.target


class _FakeEC2:
    def __init__(self, state: str) -> None:
        self.state, self.calls = state, []

    def describe_instances(self, **_: Any) -> dict[str, Any]:
        return {"Reservations": [{"Instances": [{"InstanceId": "i-1",
                                                 "State": {"Name": self.state}}]}]}

    def start_instances(self, **_: Any) -> None:
        self.calls.append("start")
        self.state = "pending"

    def stop_instances(self, **_: Any) -> None:
        self.calls.append("stop")
        self.state = "stopping"

    def get_waiter(self, name: str) -> _Waiter:
        return _Waiter(self, {"instance_running": "running", "instance_stopped": "stopped"}[name])


class _FakeSSM:
    class exceptions:  # noqa: N801 - boto3's shape
        class InvocationDoesNotExist(Exception):
            pass

    def __init__(self, logs: list[str], start_out: str = f"SIS_STARTED {RUN}") -> None:
        self.logs, self.start_out, self.sent, self._out = logs, start_out, [], ""

    def describe_instance_information(self, **_: Any) -> dict[str, Any]:
        return {"InstanceInformationList": [{"PingStatus": "Online"}]}

    def send_command(self, **kw: Any) -> dict[str, Any]:
        commands = kw["Parameters"]["commands"]
        self.sent.append(commands)
        joined = "\n".join(commands)
        if "tmux new-session" in joined:
            self._out = self.start_out
        elif "awk" in joined:
            self._out = self.logs.pop(0) if len(self.logs) > 1 else self.logs[0]
        else:
            self._out = ""
        return {"Command": {"CommandId": str(len(self.sent))}}

    def get_command_invocation(self, **_: Any) -> dict[str, Any]:
        return {"Status": "Success", "StandardOutputContent": self._out}


def _run(ec2: _FakeEC2, ssm: _FakeSSM, **kw: Any) -> Any:
    box = sr.Box(ec2, ssm, sleep=lambda s: None)
    ticks = iter(range(0, 10**6, 60))
    return sr.run_once(box, _cfg(), clock=lambda: float(next(ticks)), say=lambda m: None, **kw)


def test_a_run_that_holds_is_stopped_and_the_box_is_stopped() -> None:
    ec2 = _FakeEC2("stopped")
    ssm = _FakeSSM([_log("[sis] HOLDING: PR 23 is open"),
                    _log("[sis] HOLDING: PR 23 is open", "[loop] stopped after 0 cycle(s): x",
                         "[sis] artifacts synced to s3://b/r", f"[scheduled] {RUN} exit 0")])
    out = _run(ec2, ssm)
    assert out.interrupted and out.instance_stopped and out.error is None
    assert out.state.phase is sr.Phase.EXITED and out.state.synced
    assert ec2.calls == ["start", "stop"]
    assert any("C-c" in " ".join(c) for c in ssm.sent)


def test_a_box_that_is_already_running_is_never_touched() -> None:
    ec2 = _FakeEC2("running")
    with pytest.raises(sr.Refused):
        _run(ec2, _FakeSSM([""]))
    assert ec2.calls == []


def test_a_loop_that_refuses_to_start_still_stops_the_box() -> None:
    ec2 = _FakeEC2("stopped")
    out = _run(ec2, _FakeSSM([""], start_out="SIS_REFUSED: a loop is already running"))
    assert not out.started and out.error and "did not start" in out.error
    assert ec2.calls == ["start", "stop"] and out.instance_stopped


def test_a_loop_that_never_stops_is_abandoned_and_the_box_still_stops() -> None:
    ec2 = _FakeEC2("stopped")
    out = _run(ec2, _FakeSSM([_log("[cycle] running forever")]), limit_s=120, grace_s=120)
    assert out.interrupted and out.abandoned and out.instance_stopped


def test_the_report_says_when_the_box_was_not_stopped() -> None:
    out = sr.Outcome(started=True, instance_stopped=False)
    assert "instance stopped: NO, take it down by hand" in sr.report(_cfg(), out)


# --- an ephemeral box: built with tofu, destroyed after (OMNI-169) -------------

def test_box_vars_check_every_value_that_reaches_tofu() -> None:
    assert sr.BoxVars(run_id=RUN).args() == ["-var=region=us-east-1",
                                             "-var=name_prefix=sis-first-run",
                                             f"-var=run_id={RUN}"]
    v = sr.BoxVars(run_id=RUN, repo_ref="develop", allow_branch_ref=True)
    assert "-var=repo_ref=develop" in v.args() and "-var=allow_branch_ref=true" in v.args()
    assert "-var=repo_ref=v0.3.8" in sr.BoxVars(run_id=RUN, repo_ref="v0.3.8").args()
    for bad in [{"repo_ref": "develop"},  # a branch, not said out loud (OMNI-63)
                {"repo_ref": "v1 -var=x=y", "allow_branch_ref": True},
                {"repo_ref": "../x", "allow_branch_ref": True},
                {"region": "us-east-1 -x"}, {"name_prefix": "A b"}, {"run_id": "now"}]:
        with pytest.raises(ValueError):
            sr.BoxVars(**{"run_id": RUN, **bad})


def test_tofu_keeps_its_state_in_the_bucket_and_never_asks() -> None:
    cmds = sr.tofu_commands(sr.BoxVars(run_id=RUN), "sis-first-run-artifacts-123456789012")
    assert "-backend-config=bucket=sis-first-run-artifacts-123456789012" in cmds["init"]
    assert f"-backend-config=key={sr.STATE_KEY}" in cmds["init"]
    for step in ("apply", "destroy"):
        assert "-auto-approve" in cmds[step] and "-input=false" in cmds[step]
    with pytest.raises(ValueError):
        sr.tofu_commands(sr.BoxVars(run_id=RUN), "Bad Bucket")


def _zip_with_tofu(tmp_path: pathlib.Path) -> bytes:
    import zipfile

    z = tmp_path / "t.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("tofu", "#!/bin/sh\necho tofu\n")
    return z.read_bytes()


def test_tofu_is_installed_only_with_the_pinned_checksum(tmp_path: pathlib.Path,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    import hashlib

    data = _zip_with_tofu(tmp_path)
    arch = sr.tofu_arch()
    with pytest.raises(RuntimeError, match="is not the pinned"):
        sr.install_tofu(tmp_path / "cache", fetch=lambda url: data)
    assert not (tmp_path / "cache" / f"tofu-{sr.TOFU_VERSION}-{arch}" / "tofu").exists()
    monkeypatch.setitem(sr.TOFU_SHA256, arch, hashlib.sha256(data).hexdigest())
    urls: list[str] = []
    path = sr.install_tofu(tmp_path / "cache", fetch=lambda url: urls.append(url) or data)
    assert path.read_text().startswith("#!/bin/sh") and path.stat().st_mode & 0o100
    assert urls == [f"https://github.com/opentofu/opentofu/releases/download/"
                    f"v{sr.TOFU_VERSION}/tofu_{sr.TOFU_VERSION}_linux_{arch}.zip"]
    assert sr.install_tofu(tmp_path / "cache", fetch=lambda url: b"never") == path  # cached


def test_the_bootstrap_check_is_valid_shell_and_reads_three_states(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "boot.sh"
    path.write_text("\n".join(sr.bootstrap_commands()) + "\n")
    assert subprocess.run(["bash", "-n", str(path)], check=False).returncode == 0
    assert sr.boot_state("SIS_BOOT done\n") == "done"
    assert sr.boot_state("SIS_BOOT failed\nE: apt") == "failed"
    assert sr.boot_state("") == "running"


class _EphemeralEC2:
    """No box until tofu applies; the box is gone again once tofu destroys."""

    def __init__(self, exists: bool = False) -> None:
        self.exists = exists

    def describe_instances(self, **_: Any) -> dict[str, Any]:
        inst = [{"InstanceId": "i-new", "State": {"Name": "running"}}] if self.exists else []
        return {"Reservations": [{"Instances": inst}]}


class _BootSSM(_FakeSSM):
    def __init__(self, logs: list[str], boot: str = "SIS_BOOT done") -> None:
        super().__init__(logs)
        self.boot = boot

    def send_command(self, **kw: Any) -> dict[str, Any]:
        out = super().send_command(**kw)
        if "cloud-init status" in "\n".join(kw["Parameters"]["commands"]):
            self._out = self.boot
        return out


class _FakeTofu:
    def __init__(self, ec2: _EphemeralEC2, *, fail: tuple[str, ...] = ()) -> None:
        self.ec2, self.fail, self.calls = ec2, fail, []

    def apply(self) -> None:
        self.calls.append("apply")
        self.ec2.exists = True
        if "apply" in self.fail:
            raise RuntimeError("tofu apply failed (1)")

    def destroy(self) -> None:
        self.calls.append("destroy")
        if "destroy" in self.fail:
            raise RuntimeError("tofu destroy failed (1)")
        self.ec2.exists = False


def _ephemeral(ec2: _EphemeralEC2, ssm: _FakeSSM, tofu: _FakeTofu, **kw: Any) -> Any:
    box = sr.Box(ec2, ssm, sleep=lambda s: None)
    ticks = iter(range(0, 10**6, 60))
    return sr.run_once(box, _cfg(), clock=lambda: float(next(ticks)), say=lambda m: None,
                       lifecycle=sr.EphemeralBox(tofu), **kw)  # type: ignore[arg-type]


_DONE = [_log("[loop] stopped after 2 cycle(s): x", "[sis] artifacts synced to s3://b/r",
              f"[scheduled] {RUN} exit 0")]


def test_an_ephemeral_run_builds_its_box_and_destroys_it() -> None:
    ec2 = _EphemeralEC2()
    tofu = _FakeTofu(ec2)
    out = _ephemeral(ec2, _BootSSM(list(_DONE)), tofu)
    assert tofu.calls == ["apply", "destroy"] and not ec2.exists
    assert out.started and out.instance_stopped and out.error is None
    assert out.state.phase is sr.Phase.EXITED and "instance destroyed: yes" in sr.report(_cfg(),
                                                                                        out)


def test_an_existing_box_is_never_adopted_and_tofu_never_runs() -> None:
    ec2 = _EphemeralEC2(exists=True)
    tofu = _FakeTofu(ec2)
    with pytest.raises(sr.Refused, match="never adopts"):
        _ephemeral(ec2, _BootSSM(list(_DONE)), tofu)
    assert tofu.calls == [] and ec2.exists


@pytest.mark.parametrize(("fail", "boot"), [(("apply",), "SIS_BOOT done"),
                                            ((), "SIS_BOOT failed\nE: apt")])
def test_a_box_that_fails_to_come_up_is_still_destroyed(fail: tuple[str, ...], boot: str) -> None:
    ec2 = _EphemeralEC2()
    tofu = _FakeTofu(ec2, fail=fail)
    out = _ephemeral(ec2, _BootSSM(list(_DONE), boot=boot), tofu)
    assert not out.started and out.error
    assert tofu.calls[-1] == "destroy" and not ec2.exists and out.instance_stopped


def test_a_destroy_that_fails_twice_is_reported_loudly() -> None:
    ec2 = _EphemeralEC2()
    tofu = _FakeTofu(ec2, fail=("destroy",))
    out = _ephemeral(ec2, _BootSSM(list(_DONE)), tofu)
    assert tofu.calls == ["apply", "destroy", "destroy"] and ec2.exists
    assert not out.instance_stopped and "destroy failed twice" in (out.error or "")
    assert "instance destroyed: NO, take it down by hand" in sr.report(_cfg(), out)


class _Done:
    def __init__(self, code: int) -> None:
        self.returncode, self.stdout, self.stderr = code, "Apply complete!", ""


def test_tofu_runs_the_box_stack_in_its_own_data_dir(tmp_path: pathlib.Path) -> None:
    seen: list[tuple[list[str], dict[str, str]]] = []

    def run(argv: list[str], **kw: Any) -> _Done:
        seen.append((argv, kw["env"]))
        return _Done(0)

    tofu = sr.Tofu(pathlib.Path("/bin/tofu"), sr.BoxVars(run_id=RUN), "b-artifacts-1",
                   data_dir=tmp_path, run=run, say=lambda m: None)
    tofu.apply()
    tofu.destroy()
    steps = [argv[2] for argv, _ in seen]
    assert steps == ["init", "apply", "destroy"]   # init once
    assert all(argv[1] == f"-chdir={sr.BOX_STACK}" for argv, _ in seen)
    assert seen[0][1]["TF_DATA_DIR"] == str(tmp_path) and seen[0][1]["TF_INPUT"] == "0"
    with pytest.raises(RuntimeError, match="tofu init failed"):
        sr.Tofu(pathlib.Path("/bin/tofu"), sr.BoxVars(run_id=RUN), "b-artifacts-1",
                data_dir=tmp_path, run=lambda argv, **kw: _Done(1), say=lambda m: None).apply()


# --- the two stacks build one box (OMNI-169) ------------------------------------

_INFRA = _PATH.parent.parent / "infra" / "aws"


def test_both_stacks_build_the_box_from_one_module() -> None:
    base = (_INFRA / "main.tf").read_text()
    box = (_INFRA / "box" / "main.tf").read_text()
    assert 'source = "./modules/box"' in base and 'source = "../modules/box"' in box
    assert 'resource "aws_instance"' not in base + box
    assert "from = aws_instance.sis" in base   # the human's existing state moves, not replaces
    assert "default     = true" in (_INFRA / "variables.tf").read_text().split(
        'variable "create_instance"')[1]


def test_the_box_stack_keeps_locked_state_in_s3() -> None:
    versions = (_INFRA / "box" / "versions.tf").read_text()
    assert 'backend "s3"' in versions and "use_lockfile = true" in versions
    assert (_INFRA / "box" / ".terraform.lock.hcl").exists()


def test_the_runner_policy_can_build_the_box_but_grants_no_iam_or_secret() -> None:
    import json

    policy = json.loads((_INFRA / "scheduled_runner_policy.json").read_text())
    actions = [a for st in policy["Statement"] for a in
               ([st["Action"]] if isinstance(st["Action"], str) else st["Action"])]
    assert "ec2:RunInstances" in actions and "ec2:TerminateInstances" in actions
    assert all(st["Effect"] == "Allow" for st in policy["Statement"])
    # No role or user can be made, and the run's secret stays the box's to read.
    assert {a for a in actions if a.startswith("iam:")} == {"iam:PassRole",
                                                             "iam:GetInstanceProfile"}
    assert not [a for a in actions if a.startswith(("secretsmanager:", "sns:"))]
    assert not [a for a in actions if a.endswith("*") and a != "ec2:Describe*"]
    for st in policy["Statement"]:
        if set(st["Action"]) & {"ec2:TerminateInstances", "ec2:RunInstances"} and \
                "instance/*" in str(st["Resource"]):
            assert "sis-first-run" in json.dumps(st["Condition"])   # only the tagged box
        if "s3:PutObject" in st["Action"]:
            assert st["Resource"].endswith("/tofu/*")              # state, never the runs
        if "image/*" in str(st["Resource"]):
            # Canonical's AMIs are aliased `amazon`, which is what ec2:Owner sees.
            assert "amazon" in st["Condition"]["StringEquals"]["ec2:Owner"]
