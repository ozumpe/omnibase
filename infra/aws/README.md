# infra/aws

Terraform config for the OMNI-29 one-node run. **Read `docs/AWS_RUN.md` first**
— it is the design note and the runbook; this directory is only its
implementation.

**Driven with [OpenTofu](https://opentofu.org) (`tofu`), not HashiCorp
Terraform.** Same HCL, same `hashicorp/aws` provider, same state format — the
difference is the licence: Terraform moved to BSL-1.1 at v1.6, OpenTofu is the
MPL-2.0 fork. This project already picked Ray over Akka to stay clear of BSL
(`DESIGN.md` §2), so applying that stance to its own infra tooling costs
nothing and keeps the repo's licensing story consistent. `terraform` still
works on these files if you prefer it; the committed `.terraform.lock.hcl`
records provider hashes from OpenTofu's registry, so it would rewrite that one
file back.

```bash
tofu init
echo 'alert_email = "you@example.com"' > terraform.tfvars   # gitignored
tofu plan
tofu apply
```

State is local and gitignored, like `terraform.tfvars`.

**Two stacks, one box** (OMNI-169). The instance is `modules/box`. This stack
builds it while `create_instance` is true, the default and the human runbook.
`box/` builds it alone, for scheduled runs: `scripts/aws_scheduled_run.py
--box ephemeral` applies and destroys it, with its state and lock in the
artifacts bucket, under keys limited to `scheduled_runner_policy.json`. Set
`create_instance = false` here before using `box/`, so this stack keeps only
the base (secret, pager, bucket, role, group, budget) and two boxes never share
a Name tag. See `docs/AWS_RUN.md` "Scheduled runs". The Secrets Manager
secret is created empty on purpose: put the value in with
`scripts/aws_secret.py --upload` (see the `put_secret_hint` output), never
through the config, so it never enters state. The whole run-day sequence is at
the top of `docs/AWS_RUN.md`; `scripts/rehearse_aws_run.sh` rehearses the box
locally without AWS.
