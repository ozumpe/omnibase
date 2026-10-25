# infra/aws/box — the run box alone, for scheduled runs (OMNI-169).
#
# scripts/aws_scheduled_run.py applies this before a run and destroys it after,
# with keys that can launch and terminate this one box and nothing else. The
# base it stands on (infra/aws with create_instance = false: group, role,
# secret, bucket, pager, budget) is applied by a human, once.
#
# State lives in the artifacts bucket, not on a laptop, so whichever task runs
# next finds what the last one left. The backend is configured at init:
#   tofu -chdir=infra/aws/box init \
#     -backend-config=bucket=sis-first-run-artifacts-<account-id> \
#     -backend-config=key=tofu/box.tfstate -backend-config=region=us-east-1
# use_lockfile is S3's own lock (OpenTofu 1.10), so no DynamoDB table.

terraform {
  required_version = ">= 1.10"

  backend "s3" {
    use_lockfile = true
    encrypt      = true
  }

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}
