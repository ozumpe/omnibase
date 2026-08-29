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

State is local and gitignored, like `terraform.tfvars`. The Secrets Manager
secret is created empty on purpose: put the value in with
`aws secretsmanager put-secret-value` (see the `put_secret_hint` output), never
through the config, so it never enters state.
