# AWS run (OMNI-29) — one node, a few supervised cycles

The full loop — real Claude proposer, real Confluence/Jira/GitHub adapters,
kernel-enforced docker sandbox — on one EC2 instance, for a few cycles a human
starts, watches and stops. It proves that the engine's `SIS_ENV=aws` path, the
docker sandbox and the real adapters hold up off the laptop, and it produces an
episodic log worth keeping. Ticket:
[OMNI-29](https://olafzumpe.atlassian.net/browse/OMNI-29).

**This is not RUNBOOK Level 4.** Level 4 is the *autonomous* server: real
monitor trigger, error-driven cycles, unattended operation. This run is the
opposite on purpose: no systemd unit, no autostart, no autoscaling.

This document has four parts:

1. [Run day](#run-day): the steps, in order. Start here.
2. [Troubleshooting](#troubleshooting): everything that has gone wrong before.
3. [How the box is built](#how-the-box-is-built): why each piece is the way it is.
4. [History](#history): what each run and rehearsal found.

## At a glance

| | |
|---|---|
| Code | release tag **`v0.3.3`** (`var.repo_ref`), never a branch — see [Which code runs](#which-code-runs) |
| Box | one `m7i.xlarge`, `us-east-1`, **no inbound ports**; shell via SSM only |
| Contract | one with room left: `sort` and `sum_of_divisors` have both converged, so re-seed a naive one on `ozumpe/testrun`'s `develop` first ([Before every run](#before-every-run-a-target-with-room-left)) |
| Where artifacts land | Jira `TES`, GitHub `ozumpe/testrun` (PRs against `develop`) |
| Spend brakes | `SIS_BUDGET_USD=1.00` in the loop; an AWS Budget alarm (default $25/month) on the account |
| Cost | about $0.20 an hour while the instance runs |
| Latest | run #5 (`v0.3.2`, both contracts converged), 2026-09-29 — see [History](#history) |

---

## Run day

Each step says where it runs: **laptop** (the repo root on your machine) or
**box** (an SSM session on the instance, as `ubuntu`).

### Before the first run (one time)

Already done if you have run this before; skip to
[Before every run](#before-every-run-a-target-with-room-left).

1. **Credentials** (laptop), three of them:
   - Atlassian API token (id.atlassian.com → Security → API tokens) →
     `atlassian.api_token` in `secrets.local.yml`.
   - GitHub fine-grained PAT for `ozumpe/testrun` only, with **Contents** and
     **Pull requests** read/write → `github.token` in `secrets.local.yml`.
   - An Anthropic API key → `export ANTHROPIC_API_KEY=...` in the shell you use
     for [step 2](#2-upload-the-secret-laptop). An `ant auth login` profile
     won't do: the box needs a key.

   `secrets.local.yml` must route to the scratch tenant (`TES`,
   `ozumpe/testrun`), never `OMNI` or `ozumpe/omnibase`; the upload helper
   refuses otherwise.
2. **Prove them, read-only** (laptop). Every line should be ✓:
   ```bash
   SIS_ADAPTERS=real poetry run python scripts/check_connections.py --deep
   ```
3. **Install the SSM plugin** (laptop), which `aws ssm start-session` needs:
   ```bash
   brew install --cask session-manager-plugin
   ```
4. **Set the alert address** (laptop). The file is gitignored:
   ```bash
   echo 'alert_email = "olaf.zumpe@gmail.com"' > infra/aws/terraform.tfvars
   ```

### Before every run: a target with room left

**Once**, prepare `ozumpe/testrun` for staged delivery (done):

- a `develop` branch, created from `main`;
- `github.default_base: develop` in `secrets.local.yml`, so the loop forks
  features from `develop` and opens their PRs against it.

**Every run.** A target that has converged stops the loop after
`loop.converged_after` (default 3) attempts, and merging a run's PR is what
converges it: after run #5, `develop` holds the optimised `sort` and
`sum_of_divisors`. Put the naive baselines back, from this repo's own
`runtime/` (they are the committed baselines), with no loop PR open:

```bash
git clone git@github.com:ozumpe/testrun.git ../testrun     # first time only
cd ../testrun && git switch develop && git pull
cp ../omnibase/runtime/target.py runtime/target.py            # naive sum_of_divisors
cp ../omnibase/runtime/sort_target.py runtime/sort_target.py  # naive sort
git commit -am "Re-seed the naive targets" && git push origin develop
```

Then run the contract you re-seeded (`--contract sum_of_divisors` or
`--contract sort`) in [step 5c](#5-the-run-itself-box).

### 0. Rehearse, if the box changed (laptop, optional)

If you have changed `scripts/aws_bootstrap.sh`, `Dockerfile.gauntlet` or
`sis/gauntlet.py` since the last rehearsal, run the whole box locally first.
It needs Docker only, no AWS, and takes about five minutes (a cold Docker
volume adds a few more):

```bash
scripts/rehearse_aws_run.sh
```

It must end with `PASS`.

### 1. Stand up the box (laptop)

The release tag in `var.repo_ref` must already exist on GitHub, because
`user_data` clones it. A box applied before its tag exists is stuck; see
[Troubleshooting](#troubleshooting).

```bash
tofu -chdir=infra/aws init      # first time on this machine only
tofu -chdir=infra/aws apply     # ~1 min; the box then bootstraps for ~10 min
```

After an earlier `tofu destroy`, `apply` reports **12 to add**. The artifacts
bucket is not among them, because it survives the destroy and stays in the
local state. If `init` times out or `apply` wants to create the bucket, see
[Troubleshooting](#troubleshooting).

**Moving a running box to a new release** is the same `apply`, run from a
checkout where `var.repo_ref` names the new tag. It **replaces the instance**:
everything under `runtime/` goes with the old box, so make sure it has synced
first ([step 5d](#5-the-run-itself-box)). The secret and the pager subscription
stay, so steps 2 and 3 are not needed again. The new box re-reads from GitHub
which of the loop's PRs are still open, and waits for them (OMNI-136).

### 2. Upload the secret (laptop)

Builds the secret from `secrets.local.yml` + `$ANTHROPIC_API_KEY` and uploads
it directly. No plaintext JSON file is written, and nothing is printed but the
routing and ✓/✗. Run it without `--upload` first to see what it would send:

```bash
poetry run python scripts/aws_secret.py
poetry run python scripts/aws_secret.py --upload
```

Upload again after every `tofu destroy`: the destroy deletes the secret, and
`apply` recreates it empty.

### 3. Confirm the pager (laptop) — don't click the link

`apply` subscribes `alert_email` to the alerts topic, and AWS mails a
confirmation link. Until a subscription is confirmed, SNS accepts every
publish and delivers nothing, so a breaker trip, a spend-cap hit or a broken
sandbox would page nobody.

**Don't click the link** — confirm from the terminal instead, with
unsubscribing locked behind AWS authentication. Clicking it on 2026-09-27 was
followed each time by an unsubscribe (see [History](#history)).

1. Find the "AWS Notification - Subscription Confirmation" email (check spam
   too). Right-click **Confirm subscription**, choose "Copy link address", and
   copy nothing else afterwards.
2. Confirm (macOS: `pbpaste` reads the clipboard):
   ```bash
   aws sns confirm-subscription --region us-east-1 \
     --topic-arn "$(tofu -chdir=infra/aws output -raw alerts_topic_arn)" \
     --authenticate-on-unsubscribe true \
     --token "$(pbpaste | python3 -c 'import sys,urllib.parse as u; q=u.parse_qs(u.urlparse(sys.stdin.read().strip()).query); q=q if "Token" in q else u.parse_qs(u.urlparse(q["q"][0]).query); print(q["Token"][0])')"
   ```
   It prints a `SubscriptionArn`.
3. Check the topic has one confirmed subscription. The output should be `1	0`:
   ```bash
   aws sns get-topic-attributes --region us-east-1 \
     --topic-arn "$(tofu -chdir=infra/aws output -raw alerts_topic_arn)" \
     --query 'Attributes.[SubscriptionsConfirmed,SubscriptionsPending]' --output text
   ```

No email, a `KeyError`, or an accidental unsubscribe: see
[Troubleshooting](#troubleshooting).

### 4. Open a session (laptop → box)

```bash
eval "$(tofu -chdir=infra/aws output -raw ssm_session)"
```

Then, as the **first command of every session**:

```bash
sudo -iu ubuntu
```

A session lands as `ssm-user`, which has no checkout, no Poetry and no docker
group ([why](#access-no-inbound-ports-at-all)). Wait until the bootstrap has
finished — the log ends with `sis bootstrap complete`:

```bash
tail -f /var/log/sis-bootstrap.log
```

### 5. The run itself (box)

All of it as `ubuntu`, in `~/omnibase`, in one shell, **and that shell lives in
tmux** (OMNI-139, in `v0.3.3`; see the note below):

```bash
tmux new-session -A -s sis
```

An SSM session ends when the laptop sleeps, the network drops, or it sits idle
(20 minutes by default), and it takes its shell, and whatever runs in that
shell's foreground, with it. tmux keeps the shell on the box instead.

- **Detach** with Ctrl-b, then d. The loop keeps running.
- **Reattach**, from a new session ([step 4](#4-open-a-session-laptop--box)),
  after `sudo -iu ubuntu`, with the same command. `-A` attaches to `sis` if it
  exists and starts it if not, so there is only ever one.
- **Scroll back** with Ctrl-b, then `[` (arrow keys), and leave with `q`. While
  a pane is scrolled back, tmux takes Ctrl-C for itself, and it stays scrolled
  back across a detach: press `q` before you press Ctrl-C to stop the loop.

What this does not survive: the instance stopping or rebooting, and possibly
the SSM agent restarting itself (unverified). If the loop is gone, start it
again. A feature in progress is carried on from its branch (OMNI-135). What the
loop had recorded is in S3 only if it had synced: on a box that runs `v0.3.2`
or earlier, sync by hand ([step 5d](#5-the-run-itself-box)) before anything
replaces the box.

A box built from `v0.3.2` or earlier has no tmux. Install it from the
`ssm-user` session, before `sudo -iu ubuntu`:

```bash
sudo apt-get -o DPkg::Lock::Timeout=600 update
sudo apt-get -o DPkg::Lock::Timeout=600 install -y tmux
```

**a. Set the environment.** Export everything **before** the first
`poetry run`: the role actors are detached Ray processes that snapshot the
environment when they are created, so an export after `bootstrap()` is
invisible to them.

```bash
cd ~/omnibase
export SIS_ENV=aws SIS_ADAPTERS=real SIS_AWS_SECRET_ID=sis/first-run/credentials
export SIS_SANDBOX=docker SIS_PROPOSER=claude
export SIS_BUDGET_USD=1.00
export ANTHROPIC_API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id sis/first-run/credentials --query SecretString --output text \
  | jq -r .anthropic.api_key)
```

`SIS_NOTIFY_SNS_TOPIC_ARN`, `SIS_ARTIFACTS_BUCKET` (the loop's own copy of the
bucket, OMNI-140) and `ARTIFACTS_BUCKET` (for the commands below) are already
set in this login shell by `/etc/profile.d/sis-run.sh`, which `user_data`
writes.

**b. Prove the wiring before spending anything.** The `Pager` line must show a
✓; it fails until [step 3](#3-confirm-the-pager-laptop--dont-click-the-link)
is done.

```bash
poetry run python main.py --show-config        # every value + which layer set it
poetry run python scripts/check_connections.py --deep
```

**c. Run the loop, and watch it.**

```bash
poetry run python -u main.py --contract sum_of_divisors --loop --loop-max-cycles 10 \
  2>&1 | tee -i -a runtime/loop.log
```

`tee` keeps the console output on disk, where a new session can follow it
without attaching (`tail -f ~/omnibase/runtime/loop.log`) and, from `v0.3.3`,
where the loop's syncs upload it (before that, the by-hand sync in
[step 5d](#5-the-run-itself-box) does). `-u` sends each line as it is
printed, because a pipe, unlike a terminal, is buffered. `-i` makes tee ignore
Ctrl-C, so that Ctrl-C stops the loop and tee still writes what the loop
prints on its way out.

What to expect:

- The first line says `[sis] contract: sum_of_divisors`.
- Each passing cycle commits a step to one feature branch
  (`[cycle] feature_step: step 1 committed to feature/tes-…`). No PR opens yet.
- After 3 steps (`loop.feature_max_steps`), or as soon as a step finds no
  further gain, **one** PR opens against `develop` on `ozumpe/testrun`, with
  every step in its description.
- The loop then **holds** until you merge or close that PR on GitHub. The next
  feature starts from `develop` afterwards. Take your time.
- It also holds while **any** PR from a `feature/` branch is open against
  `develop`, including one opened by an earlier process or box: it prints
  `[sis] HOLDING: PR <n> is open …` and waits for each in turn (OMNI-136).
- A feature's steps share one Confluence spec, one epic and one story in
  `TES`, however many cycles it takes (OMNI-135).
- A restart, or a new box, carries on a feature in progress from its branch:
  `[sis] carrying on feature/tes-… (2 step(s) committed) …` (OMNI-135, in
  `v0.3.2`). On `v0.3.1` and earlier, keep one process running: the feature
  lives in memory there, and a restart starts a new one beside the old branch.
- When the target has **converged** — `loop.converged_after` (default 3)
  attempts in a row find nothing to improve — the loop stops itself:
  `[loop] stopped …: <contract> has converged`, and a WARNING page. Nothing
  is broken and there is nothing to reset; choose another contract or target
  (OMNI-138). Before that fix, the same situation filed a bug per attempt and
  tripped the circuit breaker.

**d. The dataset keeps itself** (OMNI-140, in `v0.3.3`). The loop uploads
the episodic log, its state, the operator audit and the console log
(`runtime/loop.log`, from step c) to
`s3://<bucket>/runs/<start time>/` after every cycle
(`loop.artifact_sync_every`), and once more when it stops for any reason:
converged, breaker, `--loop-max-cycles`, Ctrl-C, or a crash. It ends with
`[sis] artifacts synced to s3://… (episodic.jsonl, …)`. A killed process or a
replaced box loses at most the cycle in flight. A slow or failing bucket prints
`[sis] WARNING: artifacts not synced …` and the loop carries on.

By hand, for `v0.3.2` and earlier, for a single `main.py` run (only `--loop`
syncs), or after a process was killed:

```bash
aws s3 sync runtime/ "s3://$ARTIFACTS_BUCKET/runs/$(date +%Y%m%d-%H%M)/" \
  --exclude "*" --include "episodic*" --include "operator_audit*" \
  --include "loop*.log"
```

### 6. While it runs (box, a second session)

Open a second session ([step 4](#4-open-a-session-laptop--box)), become
`ubuntu`, and `cd ~/omnibase`.

**Pause, resume, reset the breaker:**

```bash
poetry run python -m sis.admin status
poetry run python -m sis.admin pause --reason "<why, for the audit log>"
poetry run python -m sis.admin resume --reason "<why>"
poetry run python -m sis.admin reset-breaker --reason "<why>"   # spend is NOT reset
```

Every change is appended to `runtime/operator_audit.jsonl`, which step 5d
syncs. `pause` idles the loop without exiting; to stop it outright, reattach
(`tmux new-session -A -s sis`), press `q` if the pane is scrolled back, then
Ctrl-C, and wait for `[loop] stopped` (the loop finishes the cycle in flight
first). Closing the tmux session or its pane also stops the loop, the same way,
from `v0.3.3`; before that it kills it on the spot.

**The operator console** stays bound to loopback on the box and is reached
over port forwarding:

```bash
# On the box, as ubuntu:
cd ~/omnibase && SIS_FRONTEND_AUTH=none poetry run python -m sis.frontend
```

```bash
# On the laptop, then browse http://127.0.0.1:8080
eval "$(tofu -chdir=infra/aws output -raw frontend_port_forward)"
```

`SIS_FRONTEND_AUTH=none` is required: the committed config refuses to start
the console without it ([why](#the-operator-console-and-its-auth)).

### 7. Finish (laptop)

Between runs, **stop** the instance, once the loop has finished or been
stopped: stopping ends the tmux session, and the loop with it. A stopped
instance costs only its disk (~$3/month), keeps `runtime/`, and restarts with
everything installed; start the loop again in a new tmux session
([step 5](#5-the-run-itself-box)). A new release needs a new box, not a
restart ([step 1](#1-stand-up-the-box-laptop)).

```bash
aws ec2 stop-instances --region us-east-1 \
  --instance-ids "$(tofu -chdir=infra/aws output -raw instance_id)"
```

When the experiment is over, tear it down. The box takes `runtime/` with it,
so check that the loop's last sync ran ([step 5d](#5-the-run-itself-box)). Any
PR the loop left open on `ozumpe/testrun` is waited for by the next box
(OMNI-136).

```bash
tofu -chdir=infra/aws destroy
```

`destroy` is expected to stop with `BucketNotEmpty` at the artifacts bucket:
everything else is gone, and the bucket keeps its logs unless you empty it
deliberately. It deletes the secret immediately, so re-upload it
([step 2](#2-upload-the-secret-laptop)) after the next `apply`.

---

## Troubleshooting

**`tofu init` times out reaching `registry.opentofu.org`** (first run). Raise
the timeout:
```bash
TF_REGISTRY_CLIENT_TIMEOUT=60 tofu -chdir=infra/aws init
```

**`apply` tries to create the artifacts bucket, which already exists.** The
local `terraform.tfstate` was lost; the bucket survived an earlier destroy.
Import it:
```bash
tofu -chdir=infra/aws import aws_s3_bucket.artifacts sis-first-run-artifacts-<account-id>
```

**`tofu plan` refuses the ref.** `var.repo_ref` is a branch. A run should name
a release tag; to run a branch anyway, add `-var allow_branch_ref=true` next to
`-var repo_ref=<branch>`.

**No confirmation email.** Subscribing the same address again re-sends the
confirmation for the pending subscription. It creates no second subscription,
and tofu sees no drift:
```bash
aws sns subscribe --region us-east-1 --protocol email --return-subscription-arn \
  --topic-arn "$(tofu -chdir=infra/aws output -raw alerts_topic_arn)" \
  --notification-endpoint "$(sed -nE 's/^alert_email *= *"(.*)"/\1/p' infra/aws/terraform.tfvars)"
```

**`KeyError` from Python while confirming.** The clipboard does not hold the
confirmation link. Copy it again, and nothing after it.

**Unsubscribed by accident.** `list-subscriptions-by-topic` shows `Deleted`,
and there is nothing left to confirm. Run `tofu apply` again to recreate the
subscription, or change `alert_email` (an address change replaces the
subscription and updates the budget alerts in place). Then repeat
[step 3](#3-confirm-the-pager-laptop--dont-click-the-link).

**`Pager` fails in `check_connections.py`, or the engine warns about it at
startup.** The subscription is not confirmed yet: do
[step 3](#3-confirm-the-pager-laptop--dont-click-the-link).

**`poetry: command not found`, or docker says permission denied.** You are
still `ssm-user`: run `sudo -iu ubuntu`. Use `-i`: the login shell is what
puts `~/.local/bin`, where Poetry lives, on the path.

**The loop prints `[sis] HOLDING: PR ...` and waits.** One of the loop's PRs
is still open on `ozumpe/testrun`, possibly from an earlier process or box; the
line names the others that are open too. Merge or close them, and the loop
carries on (OMNI-126, OMNI-136).

**`[sis] HOLDING: could not list open PRs (...)`.** GitHub did not answer, or
the token lacks **Pull requests: read**. The loop starts nothing until it can
check, rather than risk proposing beside an open PR; it retries every tick.

**The bootstrap log ends with `fatal: Remote branch vX.Y.Z not found in upstream
origin`.** `apply` ran before the release tag was pushed, so `user_data` could
not clone it. The box will never recover by itself: `user_data` runs on first
boot only, and a plain `apply` finds nothing to change. Push the tag
([step 1](#1-stand-up-the-box-laptop)), then replace the instance:
```bash
tofu -chdir=infra/aws apply -replace=aws_instance.sis
```
The same command replaces any box whose bootstrap failed part-way. It loses
`runtime/`, so if the box ran a cycle first, sync it
([step 5d](#5-the-run-itself-box)) before replacing it: run #5's first cycle
was lost that way.

**`destroy` stops with `BucketNotEmpty`.** Expected; see
[step 7](#7-finish-laptop).

---

## How the box is built

Everything is in `infra/aws/`: a small Terraform config, readable in one
sitting, driven with **OpenTofu** (`tofu`) rather than HashiCorp Terraform.
It uses the same HCL and the same `hashicorp/aws` provider, but it is MPL-2.0
rather than BSL-1.1, consistent with the licensing stance `DESIGN.md` §2 took
for the runtime. See `infra/aws/README.md`.

### Why AWS

Not because AWS is better. The `SIS_ENV=aws` + Secrets Manager code path
already exists and is tested (`sis/settings.py`, `scripts/check_connections.py
--deep`), boto3 ships in the `real` group, and `adapters.aws_region` is a
schema key; any other cloud means rewriting the secrets layer for no
functional gain. The workload is also the least lock-in-prone shape there is:
**one VM with a Docker daemon**. The kernel-enforced sandbox
(`SIS_SANDBOX=docker`, required for a real proposer) needs real Docker, which
rules out the PaaS options (Fargate, Cloud Run, Fly) on every cloud. Once it is
"a plain VM", clouds are interchangeable, and the tiebreaker is which one the
code already speaks.

If this ever becomes an always-on personal box rather than burst runs, a
Hetzner or DigitalOcean VM is 3–5× cheaper, and the migration is trivial for
the same reason.

### Shape

One `m7i.xlarge` (4 vCPU / 16 GiB), Ubuntu 24.04, in the default VPC,
`us-east-1` (the `adapters.aws_region` default). A cycle runs `mypy --strict`
repeatedly inside the sandbox, the sandbox is capped at `sandbox.cpus` = 2, and
Ray wants headroom beside it; on a 2-vCPU instance those all contend. About
$0.20/hour on-demand. **On-demand, not spot**: a spot interruption mid-cycle is
a debugging session nobody needs yet. Revisit when runs are routine.

| Resource | Why |
|---|---|
| `aws_instance` | the run box; IMDSv2 required, encrypted gp3 root |
| `aws_security_group` | **zero ingress rules**, all egress |
| IAM role + instance profile | SSM core + read one secret + write one bucket |
| `aws_secretsmanager_secret` | the shell only — the **value never enters Terraform** |
| `aws_s3_bucket` | run artifacts (the episodic log), versioned, public access blocked |
| `aws_sns_topic` + email subscription | the pager (OMNI-62) |
| `aws_budgets_budget` | the infra-side spend alarm (80% and 100% of a monthly cap) |

State stays local (`*.tfstate` is gitignored, like `terraform.tfvars`). One
human, one box: a remote state backend is ceremony this doesn't need yet.

### Which code runs

The box runs the **release tag** in `var.repo_ref` — `v0.3.3` by default
(OMNI-63). A tag, not `develop`: a run's results are only worth something if
they name the code that produced them, and a branch names whatever it pointed
at when the box booted. `tofu plan` refuses a branch unless
`allow_branch_ref = true`. The bootstrap log's first line and every run's
provenance (`[sis] running ...` on startup, the SelfModel's `code` record,
`code_version` in the episodic state) name the exact commit, with `-dirty` if
the tree was edited on the box.

A new `repo_ref` builds a **new** box (`user_data_replace_on_change`,
OMNI-137). `repo_ref` appears only in `user_data`, and cloud-init runs
`user_data` on an instance's first boot only. Without the setting, a changed
tag merely stopped and started the same instance: it came back on the old tag,
its loop killed, while `apply` reported success.

A run needs a contract with room left: a feature takes several steps, and a
converged target stops the loop after `loop.converged_after` attempts
(OMNI-138). A naive `sum_of_divisors` improved in four steps in July and in two
in run #5; a naive `sort` converged at once in run #5, having been optimised
earlier. Re-seed before each run ([Before every
run](#before-every-run-a-target-with-room-left)). `--contract` sets
`contracts.default` before bootstrap, so the role actors see it.

### Access: no inbound ports, at all

The security group has no ingress rules: not port 22, not 8000, not 8080.
Shell access is **SSM Session Manager**, which the instance's agent
(preinstalled on Ubuntu AMIs) initiates outbound; it is IAM-gated and logged
in CloudTrail. No SSH keypair exists to leak, and nothing listens for the
internet to find.

**A session starts as `ssm-user`, not `ubuntu`.** The agent creates that
account itself, and it owns none of what the bootstrap installed: `~` is
`/home/ssm-user`, so there is no `omnibase` checkout, no Poetry on `PATH`, and
no membership in the `docker` group the sandbox needs. `ssm-user` has
passwordless sudo, so `sudo -iu ubuntu` always works. Use the `-i` login form
rather than `sudo -u ubuntu`: Ubuntu's stock `~/.profile` is what puts
`~/.local/bin`, and therefore Poetry (installed there by `uv`), on the path.

### The operator console and its auth

The console (OMNI-28) stays loopback-bound on the box, which its own
`check_servable()` enforces for `auth: none`, and is reached over SSM port
forwarding ([step 6](#6-while-it-runs-box-a-second-session)).

`SIS_FRONTEND_AUTH=none` is not optional there. The committed `config.yml`
ships `forbidden_auth: "github"` with an empty `forbidden_allowed_logins`, so
`check_servable()` refuses to start: a GitHub login screen that admits nobody
reads as a broken deployment, not a missing setting. Both keys are
`forbidden_`, which closes the two obvious routes: the operator UI won't edit
them (that would be escalation through its own front door), and a test pins
every `forbidden_` key in the committed file to its default. **The environment
layer is the only way in, by design** — a one-run choice that leaves the
shipped guardrail untouched. Turning authentication off is safe *only* because
the bind is loopback and the security group has no ingress; `check_servable()`
enforces exactly that pairing.

For real auth on the box instead: register a GitHub OAuth app, put its
credentials in the run's Secrets Manager document alongside the rest, and set
`SIS_FRONTEND_ALLOWED_LOGINS`. Not needed while the console is reachable only
through an SSM tunnel you already authenticated to. This answers
`docs/OPERATOR_FRONTEND.md`'s "expose publicly at all?" question for this
milestone: no. Caddy, TLS and OAuth stay parked until something actually has
to be public.

### Identity and secrets

The instance gets an **IAM role**: no AWS keys on disk, ever. The role holds
the SSM managed policy (session access), `secretsmanager:GetSecretValue` on the
one secret, write access to the one artifacts bucket, and publishing to (and
reading the attributes of) the one alerts topic.

The secret is a single JSON document in the same shape as `secrets.local.yml`
(nested form; `sis/settings.py` flattens either), plus one key that
`secrets.local.yml` keeps in the environment locally: `anthropic: {api_key:
...}`, exported at run time ([step 5a](#5-the-run-itself-box)).
`sis/settings.py` ignores keys it doesn't recognise, so carrying it in the same
secret is safe. Terraform creates the empty secret; **the value is set
out-of-band, so it never enters Terraform state**, by
`scripts/aws_secret.py --upload`, which calls `put-secret-value` directly.

It uses the **same scratch tenant as Level 2** — `github.repo` pointing at
`ozumpe/testrun`, `atlassian.jira_project: TES` — because the loop files real
artifacts, and they belong in the sandbox project, not `OMNI`. The helper
refuses to upload a document routed at `OMNI` or at `ozumpe/omnibase`.

**Why the docker sandbox is non-negotiable here.** On EC2, the instance role's
credentials are served by the metadata endpoint (IMDS) to any process on the
host with network access. The gauntlet's `--network none` is what stands
between LLM-written candidate code *in the gauntlet* and that endpoint.
IMDSv2's hop limit of 1 does not help a host process (it only stops containers
behind a bridge), so the real guarantee is the sandbox. Locally,
`SIS_ALLOW_UNSANDBOXED_LLM=1` means "candidate code can read my home
directory"; on this box it would mean "candidate code can mint my cloud
credentials". Never set it here.

**The Serve canary is not covered by any of that** (KNOWN_ISSUES H3/M19). A
`--canary serve` green replica is an ordinary Ray worker on the host: no
`--network none`, so IMDS is reachable, and other processes' environments are
readable through `/proc`. The loop therefore refuses `canary.backend=serve`
with any proposer but the stub (OMNI-49) — at startup, per cycle, and at the
deployment itself — until OMNI-48 isolates the replica. Run day uses the legacy
canary (`canary.backend` unset), which never executes candidate code.

### Two spend brakes, deliberately independent

- **LLM side:** `SIS_BUDGET_USD`, the CEO brake (M5), enforced *in the loop*,
  per run, before each proposal. Tiny (`1.00`) for these runs.
- **Infra side:** the AWS Budget, enforced *by billing*, monthly, alerting at
  80% and 100% of the cap (default $25). Budget data lags by hours, so it is a
  backstop against a forgotten instance, not a real-time kill; the real-time
  control is that this stack is one instance and you stop it when you leave.
  It has no cost filter, so it watches the **whole account's** bill.

They fail independently: a runaway loop is caught by the CEO brake whatever
AWS billing knows, and a forgotten instance is caught by the budget whatever
the loop thinks it spent.

The CEO brake also **fails closed** (OMNI-61): if `runtime/episodic_state.json`
exists but cannot be read, the CEO boots with the breaker open and says why,
instead of starting again at `spent=0`. `SIS_EPISODIC_STORE=none` is refused on
this box: with a real proposer and real adapters it would mean no durable cap
and no spend record.

### What persists

The most durable thing a run produces is the episodic log, "the dataset the
system learns from" (CLAUDE.md). The instance is disposable; the log is not.
From `v0.3.3` the loop syncs it itself (step 5d): every cycle, and when it
stops. Before that, and for anything that is not `--loop`,
sync by hand at the end of any session, as `ubuntu` (`ssm-user` has no
checkout to sync):

```bash
aws s3 sync ~/omnibase/runtime/ "s3://$ARTIFACTS_BUCKET/runs/$(date +%Y%m%d-%H%M)/" \
  --exclude "*" --include "episodic*" --include "operator_audit*" \
  --include "loop*.log"
```

`operator_audit.jsonl` rides along because it records which config key a human
changed mid-run, when, and why. On a supervised run that is the context needed
to read the episodic log correctly: a cycle's outcome means something different
if someone widened a threshold an hour earlier.

Two things `tofu destroy` does on purpose: it **stops with `BucketNotEmpty`**
at the artifacts bucket once a log has been synced, so the logs survive unless
emptied deliberately; and it **deletes the secret immediately**
(`recovery_window_in_days = 0`), since the default 30-day window keeps the name
reserved and would make the next `apply` fail.

### Bootstrap

`user_data` installs git, clones the release tag, runs
`scripts/aws_bootstrap.sh`, and logs everything to
`/var/log/sis-bootstrap.log`. It also writes `/etc/profile.d/sis-run.sh`,
which exports the pager topic and the artifacts bucket for every login shell.
The script itself lives in the repo, versioned and reviewable rather than
embedded in Terraform. It installs docker, tmux and the AWS CLI, Python 3.14 via
`uv` (standard CPython, **not** free-threaded — Ray has no `cp314t` wheels;
`uv` because 24.04's apt doesn't carry 3.14), Poetry, and
`poetry install --with real --with llm --with ui` (`ui` because the operator
console runs on the box), and it builds `sis-gauntlet:latest` from
`Dockerfile.gauntlet`. Expect about 10 minutes from `apply` to ready.

`scripts/rehearse_aws_run.sh` runs `user_data` and the bootstrap on a local
Ubuntu 24.04 container, then the run's commands as `ubuntu` through a login
shell, with the docker sandbox on a real Linux daemon: a full stub-proposer
cycle must pass every gate (`[cycle] feature_step`), a loop started in tmux
must outlive its killed client and stop cleanly on Ctrl-C (OMNI-139), and the
console must answer.
Its header lists the few ways the container deliberately differs from EC2.

### Deliberately not in this milestone

No always-on service, no autostart, no autoscaling, no multi-node Ray, no
public operator frontend, no remote Terraform state, no NAT-gateway private
subnet. The box sits in the default VPC with a public IP for *egress*; with
zero ingress rules that is equivalent in exposure to a private subnet, and
$32+ a month cheaper than the NAT gateway. Each of these becomes worth
revisiting only when the thing it serves exists: an autonomous Level-4 loop, a
second operator, a second node.

---

## History

- **2026-08-16 — designed** (PR #94).
- **2026-08-30 — pre-flighted** (PR #99). Fixed two boot-time defects, each of
  which would have cost a full instance lifecycle: `user_data` racing Ubuntu's
  `unattended-upgrades` for the dpkg lock, and SSM sessions landing as
  `ssm-user` rather than `ubuntu`. Switched from Terraform to OpenTofu.
- **2026-09-23 — rehearsed** on a local Ubuntu 24.04 box. Found two defects
  that neither the unit tests nor the #99 read-through could see:
  - **The docker sandbox could not read its own temp dir on native Linux.** The
    temp dir is `0700` and owned by the host user; the container ran as the
    image's `sandbox` uid (10001) and got `Permission denied`. Docker Desktop's
    file sharing ignores ownership, which is why every Mac run passed. Worse
    than a crash: the gate reported `mypy --strict failed`, so on EC2 every
    Claude candidate would have been rejected as badly typed, billed, filed as a
    `TES` bug, and tripped the breaker after three cycles. The container now
    runs as the host user's uid (`sis.gauntlet._container_user`, which also
    refuses root). The misattribution itself was closed later (OMNI-37).
  - **The operator console could not start on the box**: the bootstrap did not
    install the `ui` group, so `python -m sis.frontend` died with
    `ModuleNotFoundError: panel`.
- **2026-09-27 — run #1, on `v0.2.0`.** Three cycles ran; the benchmark gate
  rejected all three (ratios 1.34 / 0.93 / 1.04), the breaker tripped on the
  third, and the run spent $0.107 of its $1.00 budget. Artifacts are in
  `s3://sis-first-run-artifacts-696644743351/runs/20260927-0007/`. Findings,
  all fixed in `v0.2.1`:
  - it ran the default contract instead of `sort`, and nothing showed which
    (OMNI-121);
  - its page reached nobody: 0 confirmed subscriptions passed the preflight
    (OMNI-122);
  - `main.py` did not say why a cycle rolled back or why the loop stopped
    (OMNI-123);
  - the artifacts sync needed three attempts, with a placeholder bucket name
    (OMNI-124);
  - the first `tofu init` timed out on the registry.
- **2026-09-27 — the pager, again.** An attempt stopped at the confirmation:
  the Gmail message landed in spam, and every click on *Confirm subscription*
  was followed immediately by an unsubscribe. SNS's confirmation page carries
  an unsubscribe link that works without a login, and something followed it.
  Hence [step 3](#3-confirm-the-pager-laptop--dont-click-the-link)'s terminal
  confirmation with `--authenticate-on-unsubscribe`.
- **2026-09-27 — run #2, on `v0.2.1`, contract `sort`.** A single `main.py`
  cycle followed by the loop proposed the same change twice (testrun PRs #11
  and #12, 47 s apart): the pending PR lived only in memory and died with the
  first process. Fixed in `v0.3.0` (OMNI-126): startup restores the hold and
  prints `[sis] HOLDING: PR ...`.
- **2026-09-28/29 — run #3, on `v0.3.0`, contract `sum_of_divisors`**: the
  first run with staged delivery (OMNI-130). The first feature finished after
  one step and opened testrun #13 (09-28, 02:07 UTC), and the loop held. On
  09-29 the box was **rebuilt** (02:26 UTC); the new one knew nothing of #13,
  started a fresh feature from `develop`, and opened #14 (02:53 UTC, two
  steps) — the same file from the same base, so the two conflict (M26). The
  pending PR had lived in a file on the old box. Fixed in `v0.3.1`
  (OMNI-136): before every cycle, the loop asks GitHub which of its PRs are
  still open.
- **2026-09-29 — run #4, on `v0.3.1`, contract `sum_of_divisors`**, on a
  fresh box after #13 and #14 were closed by hand. Staged delivery worked end
  to end: three accepted steps on one branch, one PR (testrun #15), the loop
  held, a human merged it, and the loop continued from `develop`. Then
  `sum_of_divisors` had converged (~1 µs per call): three attempts in a row
  found no further gain (ratios 0.933, 3.24, 0.949 against the 0.90 margin),
  each filed a bug, and the third tripped the breaker — a CRITICAL page for a
  loop that was not broken (M27). $0.27 of $1.00 spent; the pager worked.
  Fixed in `v0.3.2` (OMNI-138): no gain from `develop` is neutral, and
  convergence is its own polite stop. Artifacts in
  `s3://sis-first-run-artifacts-696644743351/runs/20260929-0450/`.
- **2026-09-29 — run #5, on `v0.3.2`, contracts `sort` and `sum_of_divisors`**
  (reset to naive). `apply` ran before the `v0.3.2` tag was pushed, so the
  first box could not clone it and was replaced by hand (`-replace`). Then the
  fixes of run #4 held. `sort` had converged: three attempts, all `no_gain`
  (ratios 0.968, 1.025, 0.976), then "sort has converged" and a WARNING page.
  `sum_of_divisors` took two steps (245 µs → 2 µs → 1 µs); a third attempt found
  no further gain and opened testrun #16 (20:22 UTC). A human merged it at
  20:26, the loop started its next cycle 43 s later, and the second feature
  converged the same way. No bug filed, breaker untouched, two WARNING pages
  (both arrived), $0.2849 of $1.00, with spend carried across the two
  processes. One plan per feature (OMNI-135): 9 TES issues for 9 cycles,
  where run #4 filed 22 for 6. The one loss: an earlier cycle (TES-118 to 121,
  20:04 UTC, rejected for a correctness mismatch) ran on a box that was then
  replaced before its log was synced, so only its TES bug survives
  (OMNI-140). Artifacts in
  `s3://sis-first-run-artifacts-696644743351/runs/20260929-2028/`.
