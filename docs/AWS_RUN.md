# First AWS run (OMNI-29) — one node, a few cycles

**Status:** the **first run happened on 2026-09-27**, on `v0.2.0`. Three
cycles ran; the benchmark gate rejected all three, the breaker tripped and the
run spent $0.107. It ran the wrong contract, and its page reached nobody. Every
finding from it is fixed in **`v0.2.1`** (OMNI-121–125), which the next run
uses. A second attempt the same day stopped at the pager: see
[Run day](#run-day) step 5 before you click anything. History: designed
(2026-08-16, PR #94), pre-flighted (2026-08-30, PR #99: two boot-time defects
fixed, switched to OpenTofu), and **rehearsed** end to end on a local Ubuntu
24.04 box (2026-09-23: two more defects fixed, see [Rehearsal](#rehearsal)).
See [OMNI-29](https://olafzumpe.atlassian.net/browse/OMNI-29). This is the "small
AWS run — watch the provenance graph and the bill" that CLAUDE.md carried as
*not yet scheduled*: the full loop (real Claude proposer, real
Confluence/Jira/GitHub adapters, kernel-enforced docker sandbox) on one EC2
instance, for a few supervised cycles.

**This is not RUNBOOK Level 4.** Level 4 is the *autonomous* server — real
monitor trigger, error-driven cycles, unattended operation. This run is
deliberately the opposite: a human starts it, watches it, and stops it. No
systemd unit, no autostart, no autoscaling. What it proves is that the engine's
`SIS_ENV=aws` path, the docker sandbox, and the real adapters all hold up off
the laptop — and what it produces is an episodic log worth keeping.

## Run day

**Re-run checklist.** Credentials, the SSM plugin and `terraform.tfvars` are
already in place from the first run; the steps below have the details.

1. `tofu -chdir=infra/aws apply`. It builds `v0.2.1`, sends to the
   `alert_email` in `terraform.tfvars`, and reuses the artifacts bucket
   ([step 4](#run-day)).
2. When the confirmation email arrives, **don't click it**. Copy the link and
   run the `aws sns confirm-subscription … --authenticate-on-unsubscribe true`
   command, then check the topic shows `1` confirmed ([step 5](#run-day)).
3. Upload the secret: `poetry run python scripts/aws_secret.py --upload`.
4. On the box, run `check_connections.py --deep`. The `Pager` line should show
   a ✓ ([The run itself](#the-run-itself), step 2).
5. `main.py --contract sort`. The first line should say
   `[sis] contract: sort`.

**First time**, everything that needs no credentials is done and rehearsed.
What is left, in order:

1. **Three credentials** — nothing else is missing.
   - Atlassian API token (id.atlassian.com → Security → API tokens) →
     `atlassian.api_token` in `secrets.local.yml`.
   - GitHub fine-grained PAT for `ozumpe/testrun` only, **Contents** and
     **Pull requests** read/write → `github.token` in `secrets.local.yml`.
   - An Anthropic API key → `export ANTHROPIC_API_KEY=...` in the shell you use
     for step 4. (An `ant auth login` profile won't do: the box needs a key.)

   `secrets.local.yml` must route to the scratch tenant (`TES`,
   `ozumpe/testrun`); step 4's helper checks and refuses otherwise.
2. **Prove them, read-only:**
   `SIS_ADAPTERS=real poetry run python scripts/check_connections.py --deep` —
   every line ✓.
3. **Install the SSM plugin** once: `brew install --cask session-manager-plugin`
   (`aws ssm start-session` needs it).
4. **Stand it up** — from the release tag, which must already exist on
   GitHub (user_data clones it; OMNI-63):
   ```bash
   cd infra/aws
   echo 'alert_email = "you@example.com"' > terraform.tfvars   # gitignored; first time only
   tofu init && tofu apply        # ~1 min; the box then bootstraps for ~10 min
   # If init times out reaching registry.opentofu.org (it did on the first
   # run): TF_REGISTRY_CLIENT_TIMEOUT=60 tofu init
   cd ../.. && poetry run python scripts/aws_secret.py --upload
   ```
   The last line builds the secret from `secrets.local.yml` +
   `$ANTHROPIC_API_KEY` and uploads it directly — no plaintext JSON file on
   disk, nothing printed but routing and ✓/✗. Run it without `--upload` first
   to see what it would send. Upload again after every `tofu destroy`: the
   destroy deletes the secret, and `apply` recreates it empty.

   **Applying again after a `tofu destroy`** is expected to work as is.
   `terraform.tfvars` already exists, so skip the `echo`. The artifacts bucket
   survived the destroy (see [What persists](#what-persists)) and is still in
   the local state, so `apply` keeps it and creates everything else:
   **12 to add**, the bucket not among them. Only a lost `terraform.tfstate`
   breaks this: `apply` would then try to create a bucket that already
   exists. Fix that with
   `tofu import aws_s3_bucket.artifacts sis-first-run-artifacts-<account-id>`.
5. **Confirm the pager — from the terminal, not by clicking.** `tofu apply`
   subscribes `alert_email` to the `<name_prefix>-alerts` SNS topic, and AWS
   mails a confirmation link. Until a subscription is confirmed, SNS accepts
   every publish and delivers nothing, so a breaker trip, a spend-cap hit or a
   broken sandbox would page nobody (OMNI-62). The first run went ahead with
   "0 confirmed" and its page reached nobody. On the box,
   `check_connections.py`'s `Pager` line now **fails** until a subscription is
   confirmed, and the engine warns at startup (OMNI-122).

   **Don't click the link.** On 2026-09-27 the Gmail confirmation landed in
   spam, and every click on *Confirm subscription* was followed immediately by
   an unsubscribe. SNS's confirmation page carries an unsubscribe link that
   works without any login, and something followed it. Confirm from the
   terminal instead, with unsubscribing locked behind AWS authentication:

   1. Find the "AWS Notification - Subscription Confirmation" email; check
      spam too. Right-click **Confirm subscription**, choose "Copy link
      address", and copy nothing else afterwards.
   2. From the repo root (macOS: `pbpaste` reads the clipboard):
      ```bash
      aws sns confirm-subscription --region us-east-1 \
        --topic-arn "$(tofu -chdir=infra/aws output -raw alerts_topic_arn)" \
        --authenticate-on-unsubscribe true \
        --token "$(pbpaste | python3 -c 'import sys,urllib.parse as u; q=u.parse_qs(u.urlparse(sys.stdin.read().strip()).query); q=q if "Token" in q else u.parse_qs(u.urlparse(q["q"][0]).query); print(q["Token"][0])')"
      ```
      It prints a `SubscriptionArn`. A `KeyError` from Python means the
      clipboard doesn't hold the link.
   3. Check the topic now has 1 confirmed subscription:
      ```bash
      aws sns get-topic-attributes --region us-east-1 \
        --topic-arn "$(tofu -chdir=infra/aws output -raw alerts_topic_arn)" \
        --query 'Attributes.[SubscriptionsConfirmed,SubscriptionsPending]' --output text
      ```
      The output should be `1	0`.

   **No email?** Subscribing the same address again re-sends the confirmation
   for the pending subscription. It creates no second subscription, and tofu
   sees no drift:
   ```bash
   aws sns subscribe --region us-east-1 --protocol email --return-subscription-arn \
     --topic-arn "$(tofu -chdir=infra/aws output -raw alerts_topic_arn)" \
     --notification-endpoint "$(sed -nE 's/^alert_email *= *"(.*)"/\1/p' infra/aws/terraform.tfvars)"
   ```
   **Unsubscribed by accident?** Then `list-subscriptions-by-topic` shows
   `Deleted`, and there is nothing left to confirm. Run `tofu apply` again to
   recreate the subscription, or change `alert_email` (an address change
   replaces the subscription, and updates the budget alerts in place). Then
   repeat 1–3.
6. **Run it:** `tofu -chdir=infra/aws output -raw ssm_session` prints the
   session command; to open the session straight away:
   ```bash
   eval "$(tofu -chdir=infra/aws output -raw ssm_session)"
   ```
   then [The run itself](#the-run-itself).
7. **Stop the meter:** sync the episodic log (step 5 of the run), stop the
   instance; `tofu destroy` when the experiment is over (see
   [What persists](#what-persists)).

If you have changed the bootstrap, `Dockerfile.gauntlet`, or `sis/gauntlet.py`
since the last rehearsal, run `scripts/rehearse_aws_run.sh` first (~2 min,
Docker only).

## Why AWS (and why the choice stays cheap)

Not because AWS is better. The `SIS_ENV=aws` + Secrets Manager code path
already exists and is tested (`sis/settings.py`, `scripts/check_connections.py
--deep`), boto3 ships in the `real` group, and `adapters.aws_region` is a
schema key — any other cloud means rewriting the secrets layer for zero
functional gain. And the workload itself is the least lock-in-prone shape
infrastructure can have: **one VM with a Docker daemon**. The gauntlet's
kernel-enforced sandbox (`SIS_SANDBOX=docker`, required for a real proposer)
needs real Docker, which rules out the pleasant PaaS options (Fargate, Cloud
Run, Fly) on every cloud — and once you're down to "a plain VM", clouds are
interchangeable, so the tiebreaker is which one the code already speaks.

If this ever becomes an always-on personal box rather than burst runs, a
Hetzner/DigitalOcean VM is 3–5× cheaper and the migration is trivial for
exactly the same reason. Not a reason to deviate now.

## Shape

One `m7i.xlarge` (4 vCPU / 16 GiB), Ubuntu 24.04, in the default VPC,
`us-east-1` (the `adapters.aws_region` default). Sizing rationale: a cycle
runs `mypy --strict` repeatedly inside the sandbox, the sandbox itself is
capped at `sandbox.cpus` = 2, and Ray wants headroom beside it — on a 2-vCPU
instance those all contend. ~$0.20/hour on-demand; an afternoon costs about a
dollar. **On-demand, not spot**, for the first runs: a spot interruption
mid-cycle is a debugging session nobody needs yet. Revisit when runs are
routine.

Everything is in `infra/aws/` — a small Terraform config, deliberately readable
in one sitting, driven with **OpenTofu** (`tofu`) rather than HashiCorp
Terraform: same HCL and same `hashicorp/aws` provider, but MPL-2.0 rather than
BSL-1.1, which keeps the infra consistent with the licensing stance `DESIGN.md`
§2 already took for the runtime. See `infra/aws/README.md`.

| Resource | Why |
|---|---|
| `aws_instance` | the run box; IMDSv2 required, encrypted gp3 root |
| `aws_security_group` | **zero ingress rules**, all egress |
| IAM role + instance profile | SSM core + read one secret + write one bucket |
| `aws_secretsmanager_secret` | the shell only — the **value never enters Terraform** |
| `aws_s3_bucket` | run artifacts (the episodic log), versioned, public access blocked |
| `aws_budgets_budget` | the infra-side spend alarm (80% and 100% of a monthly cap) |

State stays local (`*.tfstate` is gitignored, like `terraform.tfvars`, which
holds the alert email). One human, one box; a remote state backend is ceremony
this doesn't need yet.

## Access: no inbound ports, at all

The security group has no ingress rules — not port 22, not 8000, not 8080.
Shell access is **SSM Session Manager**, which is outbound-initiated from the
instance's agent (preinstalled on Ubuntu AMIs), IAM-gated, and logged in
CloudTrail. No SSH keypair exists to leak, and nothing listens for the
internet to find:

```bash
aws ssm start-session --target <instance-id> --region us-east-1
sudo -iu ubuntu          # first command of every session — see below
```

**A session starts as `ssm-user`, not as `ubuntu`.** The agent creates that
account itself, and it owns none of what the bootstrap installed: `~` is
`/home/ssm-user`, so there is no `omnibase` checkout, no Poetry on `PATH`, and
no membership in the `docker` group the gauntlet's sandbox needs. Every command
below assumes you have become `ubuntu` first (`ssm-user` has passwordless sudo,
so this always works). Use the `-i` login form rather than `sudo -u ubuntu`:
Ubuntu's stock `~/.profile` is what puts `~/.local/bin` — and therefore
`poetry`, installed there by `uv` — on the path.

The operator console (OMNI-28) follows the same rule: it stays loopback-bound
on the box — which its own `check_servable()` enforces for `auth: none` — and
is reached over SSM port forwarding:

```bash
# On the box, as ubuntu. SIS_FRONTEND_AUTH=none is required, see below.
cd ~/omnibase && SIS_FRONTEND_AUTH=none poetry run python -m sis.frontend
```

```bash
# From your laptop.
aws ssm start-session --target <instance-id> \
  --document-name AWS-StartPortForwardingSession \
  --parameters '{"portNumber":["8080"],"localPortNumber":["8080"]}'
# then browse http://127.0.0.1:8080
```

**`SIS_FRONTEND_AUTH=none` is not optional here**, and the reason is worth
understanding rather than pasting. The committed `config.yml` ships
`forbidden_auth: "github"` with an empty `forbidden_allowed_logins`, so
`check_servable()` refuses to start — correctly, since a GitHub login screen
that admits nobody reads as a broken deployment rather than as a missing
setting. Both keys are `forbidden_`, which closes the two obvious routes: the
operator UI will not edit them (that would be escalation through its own front
door), and a test pins every `forbidden_` key in the committed file to its
default, so they cannot be changed there either. **The environment layer is the
only way in, by design** — a one-run choice that leaves the shipped guardrail
untouched. Turning authentication off is safe *only* because the bind is
loopback and the security group has no ingress; `check_servable()` enforces
exactly that pairing, and it is the whole reason this is defensible.

The alternative, if you would rather have real auth on the box: register a
GitHub OAuth app, put its credentials in the run's Secrets Manager document
alongside the rest, and set `SIS_FRONTEND_ALLOWED_LOGINS`. Not needed while the
console is reachable only through an SSM tunnel you already authenticated to.

This resolves `docs/OPERATOR_FRONTEND.md`'s "expose publicly at all?" question
for this milestone the sane way: not. Caddy/TLS/OAuth stay parked until
something actually has to be public.

## Identity & secrets

The instance gets an **IAM role** — no AWS keys on disk, ever. The role holds
exactly three permissions: the SSM managed policy (session access),
`secretsmanager:GetSecretValue` on the one secret, and write access to the one
artifacts bucket.

The secret is a single JSON document, the same shape as `secrets.local.yml`
(nested form; `sis/settings.py` flattens either), plus one key
`secrets.local.yml` keeps in the environment locally: `anthropic: {api_key:
...}`, exported at run time (below) — `sis/settings.py` ignores keys it doesn't
recognise, so carrying it in the same secret is safe. Terraform creates the
empty secret; **the value is set out-of-band so it never enters Terraform
state**, by `scripts/aws_secret.py --upload`, which builds the document from
`secrets.local.yml` + `$ANTHROPIC_API_KEY` and calls `put-secret-value`
directly — there is no intermediate JSON file to forget to delete.

Use the **same scratch tenant as Level 2**: `github.repo` pointing at
`ozumpe/testrun`, `atlassian.jira_project: TES` — the loop files real
artifacts, and they should land in the sandbox project, not `OMNI`. The helper
enforces that: it refuses to upload a document routed at `OMNI` or at
`ozumpe/omnibase`.

**Why the docker sandbox is non-negotiable here, beyond M1.** On EC2, the
instance role's credentials are served by the metadata endpoint (IMDS) to any
process on the host with network access. The gauntlet's `--network none` is
what stands between LLM-written candidate code *in the gauntlet* and that
endpoint. IMDSv2's hop limit of 1 does not help a host process — it only stops
containers behind a bridge — so the real guarantee is the sandbox: locally,
`SIS_ALLOW_UNSANDBOXED_LLM=1` means "candidate code can read my home
directory"; on this box it would mean "candidate code can mint my cloud
credentials". Don't set it here, ever.

**The Serve canary is not covered by any of that** (KNOWN_ISSUES H3/M19). A
`--canary serve` green replica is an ordinary Ray worker on the host: no
`--network none`, so IMDS is reachable, and other processes' environments are
readable through `/proc`. The loop therefore refuses `canary.backend=serve`
with any proposer but the stub (OMNI-49) — at startup, per cycle, and at the
deployment itself — until OMNI-48 isolates the replica. Run day uses the
legacy canary (`canary.backend` unset), which never executes candidate code.

## Two spend brakes, deliberately independent

- **LLM side:** `SIS_BUDGET_USD` — the CEO brake (M5), enforced *in the loop*,
  per run, before each proposal. Set it tiny (`1.00`) for the first run.
- **Infra side:** the AWS Budget — enforced *by billing*, monthly, alerting at
  80% and 100% of the cap (default $25). Know its limitation: budget data lags
  hours, so it is a backstop against a forgotten instance, not a real-time
  kill. The real-time infra control is that this stack is one instance and
  you stop it when you leave. It has no cost filter, so it watches the
  **whole account's** bill: anything else running in the account counts
  toward the cap.

They fail independently: a runaway loop is caught by the CEO brake regardless
of what AWS billing knows, and a forgotten instance is caught by the budget
regardless of what the loop thinks it spent.

The CEO brake also **fails closed** (OMNI-61): if `runtime/episodic_state.json`
exists but cannot be read, the CEO boots with the breaker open and says why,
instead of starting again at `spent=0`. `SIS_EPISODIC_STORE=none` is refused
on this box — with a real proposer and real adapters it would mean no durable
cap and no spend record.

**Pausing, resuming, resetting** — from a second SSM session, as `ubuntu`, in
`~/omnibase`, while the loop runs:

```bash
poetry run python -m sis.admin status
poetry run python -m sis.admin pause --reason "<why, for the audit log>"
poetry run python -m sis.admin resume --reason "<why>"
poetry run python -m sis.admin reset-breaker --reason "<why>"   # spend is NOT reset
```

Every change is appended to `runtime/operator_audit.jsonl`, which step 5 below
syncs to S3 with the episodic log. `pause` makes the loop idle rather than
exit; to stop it outright, Ctrl-C the loop (it finishes the cycle in flight).

## What persists

The most durable thing a run produces is the episodic log — per CLAUDE.md,
"the dataset the system learns from". The instance is disposable; the log is
not. At the end of a session:

```bash
# As ubuntu (sudo -iu ubuntu) — ssm-user has no checkout to sync.
# $ARTIFACTS_BUCKET comes from /etc/profile.d/sis-run.sh (user_data, OMNI-124).
aws s3 sync ~/omnibase/runtime/ "s3://$ARTIFACTS_BUCKET/runs/$(date +%Y%m%d-%H%M)/" \
  --exclude "*" --include "episodic*" --include "operator_audit*"
```

`operator_audit.jsonl` (OMNI-28) rides along for the same reason: it records
which config key a human changed mid-run, when, and why. On a supervised run
that is precisely the context needed to read the episodic log correctly — a
cycle's outcome means something different if someone widened a threshold an
hour earlier, and the instance it was recorded on is disposable.

Between early runs, **stop** the instance rather than terminating it — a
stopped instance costs only its EBS volume (~$3/month for 40 GB) and restarts
with everything installed. `tofu destroy` when the experiment is over. Two
things it does on purpose: it **stops with a `BucketNotEmpty` error** at the
artifacts bucket once a log has been synced — everything else is gone, and the
bucket and its logs survive unless emptied deliberately; and it **deletes the
secret immediately** (`recovery_window_in_days = 0`), since the default 30-day
window keeps the name reserved and would make the next `tofu apply` fail.

## Bootstrap

`user_data` is five lines: install git, clone the repo, run
`scripts/aws_bootstrap.sh`, log everything to `/var/log/sis-bootstrap.log`.
The script itself lives in the repo — versioned and reviewable, not embedded
in Terraform — and installs: docker + the AWS CLI, Python 3.14 via `uv`
(standard CPython, **not** free-threaded — Ray has no `cp314t` wheels; `uv`
because 24.04's apt doesn't carry 3.14), Poetry, `poetry install --with real
--with llm --with ui` (`ui` because the operator console above runs on the
box), and builds `sis-gauntlet:latest` from `Dockerfile.gauntlet`.

Expect ~10 minutes from `tofu apply` to ready. Check with:

```bash
tail -f /var/log/sis-bootstrap.log   # inside an SSM session
```

### Rehearsal

`scripts/rehearse_aws_run.sh` runs `user_data` and the bootstrap on a local
Ubuntu 24.04 container, then the run's commands as `ubuntu` through a login
shell, with the docker sandbox on a real Linux daemon: a full stub-proposer
cycle must reach `verified_awaiting_human_merge` and the console must answer.
No AWS, no credentials, about two minutes. Its header lists the few ways the
container deliberately differs from EC2.

The first rehearsal (2026-09-23) found two defects that neither the unit tests
nor the #99 read-through could see, each of which would have cost a full
instance lifecycle:

- **The docker sandbox could not read its own temp dir on native Linux.** The
  temp dir is `0700` and owned by the host user; the container ran as the
  image's `sandbox` uid (10001) and got `Permission denied`. Docker Desktop's
  file sharing ignores ownership, which is why every Mac run — including the
  first real-life test — passed. Worse than a crash: the gate reported
  `mypy --strict failed`, so on EC2 every Claude candidate would have been
  rejected as badly typed, billed, filed as a `TES` bug, and tripped the
  circuit breaker after three cycles. The container now runs as the host
  user's uid (`sis.gauntlet._container_user`, which also refuses root).
- **The operator console could not start on the box** — the bootstrap did not
  install the `ui` group, so `python -m sis.frontend` died with
  `ModuleNotFoundError: panel`.

## The run itself

The box runs the **release tag** in `var.repo_ref` — `v0.2.1` by default
(OMNI-63). A tag, not `develop`: a run's results are only worth something if
they name the code that produced them, and a branch names whatever it pointed
at when the box booted. Running a branch needs `-var allow_branch_ref=true` on
top of `-var repo_ref=<branch>`; `tofu plan` refuses otherwise. The bootstrap
log's first line (`/var/log/sis-bootstrap.log`) and every run's provenance
(`[sis] running ...` on startup, the SelfModel's `code` record,
`code_version` in the episodic state) name the exact commit, and say `-dirty`
if the tree was edited on the box.

Run day uses the **`sort`** contract (decided 2026-09-26): its target scales
with input size, so a live canary measures the function rather than the
framework. Hence `--contract sort` below — it sets `contracts.default` before
bootstrap, so the role actors see it.

```bash
sudo -iu ubuntu   # if you are not already — a session lands as ssm-user
cd ~/omnibase

# 1. Everything exported BEFORE the first poetry run — the role actors are
#    detached Ray processes that snapshot the environment at creation;
#    an export after bootstrap() is invisible to them (CLAUDE.md trap).
export SIS_ENV=aws SIS_ADAPTERS=real SIS_AWS_SECRET_ID=sis/first-run/credentials
export SIS_SANDBOX=docker SIS_PROPOSER=claude
export SIS_BUDGET_USD=1.00
export ANTHROPIC_API_KEY=$(aws secretsmanager get-secret-value \
  --secret-id sis/first-run/credentials --query SecretString --output text \
  | jq -r .anthropic.api_key)

# 2. Prove the wiring before spending anything. The Pager line must show a
#    confirmed subscription; SIS_NOTIFY_SNS_TOPIC_ARN (and ARTIFACTS_BUCKET,
#    step 5) come from /etc/profile.d/sis-run.sh (user_data), so they are set
#    in this login shell.
poetry run python main.py --show-config        # every value + which layer set it
poetry run python scripts/check_connections.py --deep

# 3. One cycle, watched. Check the first line says `[sis] contract: sort`:
#    the first run silently optimised the default contract (OMNI-121).
poetry run python main.py --contract sort

# 4. Then a short loop.
poetry run python main.py --contract sort --loop --loop-max-cycles 3

# 5. Keep the dataset, stop the meter. The first run needed three attempts
#    here with a <placeholder> bucket; the box now knows its own (OMNI-124).
aws s3 sync runtime/ "s3://$ARTIFACTS_BUCKET/runs/$(date +%Y%m%d-%H%M)/" \
  --exclude "*" --include "episodic*" --include "operator_audit*"
```

Then stop the instance from your laptop:
`aws ec2 stop-instances --instance-ids <id> --region us-east-1`.

## Deliberately not in this milestone

No always-on service, no autostart, no autoscaling, no multi-node Ray, no
public operator frontend, no remote Terraform state, no NAT-gateway private
subnet (the box sits in the default VPC with a public IP for *egress*; with
zero ingress rules that is equivalent in exposure to a private subnet and $32+
per month cheaper than the NAT gateway). Each of these becomes worth revisiting
only when the thing it serves exists — an autonomous Level-4 loop, a second
operator, a second node.
