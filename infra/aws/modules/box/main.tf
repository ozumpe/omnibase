# The run box itself (OMNI-29), shared by the base stack (infra/aws, with
# create_instance = true, the human runbook) and the box-only stack
# (infra/aws/box, which a scheduled run applies and destroys, OMNI-169).
# Everything it needs from the base is passed in by name or id, so the two
# stacks cannot drift apart.

terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
  }
}

# Canonical publishes the current Ubuntu 24.04 AMI id as a public SSM
# parameter, so nothing here pins a stale AMI by hand.
data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

resource "aws_instance" "sis" {
  ami                    = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type          = var.instance_type
  vpc_security_group_ids = [var.security_group_id]
  iam_instance_profile   = var.instance_profile

  # IMDSv2 only. Candidate code is kept away from the credential endpoint by
  # the gauntlet's --network none, not by this — but v1 has no business here.
  metadata_options {
    http_endpoint = "enabled"
    http_tokens   = "required"
  }

  root_block_device {
    volume_size = var.root_volume_gb
    volume_type = "gp3"
    encrypted   = true
  }

  # Six lines on purpose; the real bootstrap lives in the repo where it is
  # versioned and reviewed (scripts/aws_bootstrap.sh).
  #
  # DPkg::Lock::Timeout is not a nicety: Ubuntu's cloud images run
  # unattended-upgrades on the apt-daily timer at boot, which is exactly when
  # user_data runs. Without it, apt loses the race, `set -e` aborts, and the
  # box comes up with no repo and a truncated log — the classic user_data
  # failure. Waiting is the whole fix; apt >= 2.1.10 (24.04 ships 2.7) honours
  # this for its own locks as well as dpkg's.
  user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail
    exec > /var/log/sis-bootstrap.log 2>&1
    apt-get -o DPkg::Lock::Timeout=600 update
    apt-get -o DPkg::Lock::Timeout=600 install -y git
    git clone --branch ${var.repo_ref} ${var.repo_url} /home/ubuntu/omnibase
    chown -R ubuntu:ubuntu /home/ubuntu/omnibase
    echo 'export SIS_NOTIFY_SNS_TOPIC_ARN=${var.alerts_topic_arn}' > /etc/profile.d/sis-run.sh
    echo 'export ARTIFACTS_BUCKET=${var.artifacts_bucket}' >> /etc/profile.d/sis-run.sh
    echo 'export SIS_ARTIFACTS_BUCKET=${var.artifacts_bucket}' >> /etc/profile.d/sis-run.sh
    bash /home/ubuntu/omnibase/scripts/aws_bootstrap.sh
  EOT

  # A new repo_ref (every release bumps its default) must build a new box from
  # the new tag. Without this, a changed user_data only stops and starts the
  # same instance; cloud-init runs user_data on first boot only, so the box came
  # back on the old tag, its loop killed, and `apply` reported success
  # (OMNI-137). Replacing it loses runtime/ — sync the artifacts first
  # (docs/AWS_RUN.md, step 5d); open PRs are re-read from GitHub (OMNI-136).
  user_data_replace_on_change = true

  tags = merge(var.extra_tags, { Name = var.name_prefix })

  lifecycle {
    # OMNI-63: a release tag unless someone says otherwise, out loud. A
    # precondition rather than a variable validation so the rule can read both
    # variables, and so it fails at plan time — before anything is created.
    precondition {
      condition     = can(regex("^v[0-9]+\\.[0-9]+\\.[0-9]+$", var.repo_ref)) || var.allow_branch_ref
      error_message = "repo_ref must be a release tag (vX.Y.Z); to run a branch, also set allow_branch_ref = true."
    }
  }
}
