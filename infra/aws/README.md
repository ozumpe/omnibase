# infra/aws

Terraform for the OMNI-29 one-node run. **Read `docs/AWS_RUN.md` first** — it
is the design note and the runbook; this directory is only its implementation.

```bash
terraform init
echo 'alert_email = "you@example.com"' > terraform.tfvars   # gitignored
terraform plan
terraform apply
```

State is local and gitignored, like `terraform.tfvars`. The Secrets Manager
secret is created empty on purpose: put the value in with
`aws secretsmanager put-secret-value` (see the `put_secret_hint` output), never
through Terraform, so it never enters state.
