# First AWS run (OMNI-29) — one node, a few cycles

**Status:** designed + Terraformed (2026-08-16); not yet applied. See
[OMNI-29](https://olafzumpe.atlassian.net/browse/OMNI-29). This is the "small
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
(nested form; `sis/settings.py` flattens either). Terraform creates the empty
secret; **the value is set out-of-band so it never enters Terraform state or
the shell history of a committed file**:

```bash
aws secretsmanager put-secret-value \
  --secret-id sis/first-run/credentials \
  --secret-string file://secrets.aws.json && rm secrets.aws.json
```

Use the **same scratch tenant as Level 2**: `github.repo` pointing at
`ozumpe/testrun`, `atlassian.jira_project: TES` — the loop files real
artifacts, and they should land in the sandbox project, not `OMNI`. Add one key
that `secrets.local.yml` keeps in the environment locally:
`anthropic: {api_key: ...}`, exported at run time (below). `sis/settings.py`
ignores keys it doesn't recognise, so carrying it in the same secret is safe.

**Why the docker sandbox is non-negotiable here, beyond M1.** On EC2, the
instance role's credentials are served by the metadata endpoint (IMDS) to any
process on the host with network access. The gauntlet's `--network none` is
what stands between LLM-written candidate code and that endpoint. IMDSv2 is
enforced too, but the real guarantee is the sandbox: locally,
`SIS_ALLOW_UNSANDBOXED_LLM=1` means "candidate code can read my home
directory"; on this box it would mean "candidate code can mint my cloud
credentials". Don't set it here, ever.

## Two spend brakes, deliberately independent

- **LLM side:** `SIS_BUDGET_USD` — the CEO brake (M5), enforced *in the loop*,
  per run, before each proposal. Set it tiny (`1.00`) for the first run.
- **Infra side:** the AWS Budget — enforced *by billing*, monthly, alerting at
  80% and 100% of the cap (default $25). Know its limitation: budget data lags
  hours, so it is a backstop against a forgotten instance, not a real-time
  kill. The real-time infra control is that this stack is one instance and
  you stop it when you leave.

They fail independently: a runaway loop is caught by the CEO brake regardless
of what AWS billing knows, and a forgotten instance is caught by the budget
regardless of what the loop thinks it spent.

## What persists

The most durable thing a run produces is the episodic log — per CLAUDE.md,
"the dataset the system learns from". The instance is disposable; the log is
not. At the end of a session:

```bash
# As ubuntu (sudo -iu ubuntu) — ssm-user has no checkout to sync.
aws s3 sync ~/omnibase/runtime/ s3://<artifacts-bucket>/runs/$(date +%Y%m%d-%H%M)/ \
  --exclude "*" --include "episodic*" --include "operator_audit*"
```

`operator_audit.jsonl` (OMNI-28) rides along for the same reason: it records
which config key a human changed mid-run, when, and why. On a supervised run
that is precisely the context needed to read the episodic log correctly — a
cycle's outcome means something different if someone widened a threshold an
hour earlier, and the instance it was recorded on is disposable.

Between early runs, **stop** the instance rather than terminating it — a
stopped instance costs only its EBS volume (~$3/month for 40 GB) and restarts
with everything installed. `tofu destroy` when the experiment is over;
the S3 bucket and its logs survive that too unless emptied deliberately.

## Bootstrap

`user_data` is five lines: install git, clone the repo, run
`scripts/aws_bootstrap.sh`, log everything to `/var/log/sis-bootstrap.log`.
The script itself lives in the repo — versioned and reviewable, not embedded
in Terraform — and installs: docker + the AWS CLI, Python 3.14 via `uv`
(standard CPython, **not** free-threaded — Ray has no `cp314t` wheels; `uv`
because 24.04's apt doesn't carry 3.14), Poetry, `poetry install --with real
--with llm`, and builds `sis-gauntlet:latest` from `Dockerfile.gauntlet`.

Expect ~10 minutes from `tofu apply` to ready. Check with:

```bash
tail -f /var/log/sis-bootstrap.log   # inside an SSM session
```

## The run itself

The box runs whatever `var.repo_ref` pointed at when it booted — `develop` by
default. Work sitting on an unmerged feature branch is *not* on the box; merge
it first, or set `repo_ref` and re-apply. A run that silently exercises
last week's engine is the kind of result that is worse than no result.

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

# 2. Prove the wiring before spending anything.
poetry run python main.py --show-config        # every value + which layer set it
poetry run python scripts/check_connections.py --deep

# 3. One cycle, watched.
poetry run python main.py

# 4. Then a short loop.
poetry run python main.py --loop --loop-max-cycles 3

# 5. Keep the dataset, stop the meter.
aws s3 sync runtime/ s3://<artifacts-bucket>/runs/$(date +%Y%m%d-%H%M)/ \
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
