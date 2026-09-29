# One instance, one secret, one bucket, one budget. Design: docs/AWS_RUN.md.

data "aws_vpc" "default" {
  default = true
}

data "aws_caller_identity" "current" {}

# Canonical publishes the current Ubuntu 24.04 AMI id as a public SSM
# parameter, so nothing here pins a stale AMI by hand.
data "aws_ssm_parameter" "ubuntu_ami" {
  name = "/aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id"
}

# --------------------------------------------------------------------------
# Network: zero ingress. Shell access is SSM Session Manager (outbound from
# the instance's agent), so nothing listens for the internet to find — no 22,
# no 8000, no 8080. Egress stays open: the engine must reach the Anthropic
# API, Atlassian, GitHub, and Secrets Manager.
# --------------------------------------------------------------------------

resource "aws_security_group" "egress_only" {
  name        = "${var.name_prefix}-egress-only"
  description = "No inbound at all; SSM Session Manager is the only way in"
  vpc_id      = data.aws_vpc.default.id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "${var.name_prefix}-egress-only" }
}

# --------------------------------------------------------------------------
# Identity: an instance role instead of keys on disk. Exactly three grants —
# SSM sessions, read the one secret, write the one bucket.
# --------------------------------------------------------------------------

resource "aws_iam_role" "instance" {
  name = "${var.name_prefix}-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_role_policy" "least" {
  name = "${var.name_prefix}-secret-and-artifacts"
  role = aws_iam_role.instance.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadTheOneSecret"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = aws_secretsmanager_secret.credentials.arn
      },
      {
        Sid    = "WriteRunArtifacts"
        Effect = "Allow"
        Action = ["s3:PutObject", "s3:GetObject", "s3:ListBucket"]
        Resource = [
          aws_s3_bucket.artifacts.arn,
          "${aws_s3_bucket.artifacts.arn}/*",
        ]
      },
      {
        # Publish is the pager; GetTopicAttributes is check_connections.py's
        # read-only preflight. This one topic only (OMNI-62).
        Sid      = "PageTheOperator"
        Effect   = "Allow"
        Action   = ["sns:Publish", "sns:GetTopicAttributes"]
        Resource = aws_sns_topic.alerts.arn
      },
    ]
  })
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name_prefix}-instance"
  role = aws_iam_role.instance.name
}

# --------------------------------------------------------------------------
# The secret SHELL only. The value is set out-of-band (see docs/AWS_RUN.md)
# so a credential never enters Terraform state or this repo.
# --------------------------------------------------------------------------

resource "aws_secretsmanager_secret" "credentials" {
  name        = var.secret_name
  description = "sis credentials — same JSON shape as secrets.local.yml; value set via put-secret-value, never via Terraform"

  # Delete immediately on destroy. The default 30-day recovery window keeps the
  # name reserved, so apply → destroy → apply-again (the normal rhythm of early
  # runs) fails with "already scheduled for deletion". The value is only ever a
  # copy of secrets.local.yml, so there is nothing to recover.
  recovery_window_in_days = 0
}

# --------------------------------------------------------------------------
# Run artifacts: the episodic log outlives the instance.
# --------------------------------------------------------------------------

resource "aws_s3_bucket" "artifacts" {
  bucket = "${var.name_prefix}-artifacts-${data.aws_caller_identity.current.account_id}"
  tags   = { Name = "${var.name_prefix}-artifacts" }
}

resource "aws_s3_bucket_versioning" "artifacts" {
  bucket = aws_s3_bucket.artifacts.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_public_access_block" "artifacts" {
  bucket                  = aws_s3_bucket.artifacts.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# --------------------------------------------------------------------------
# The box.
# --------------------------------------------------------------------------

resource "aws_instance" "sis" {
  ami                    = data.aws_ssm_parameter.ubuntu_ami.value
  instance_type          = var.instance_type
  vpc_security_group_ids = [aws_security_group.egress_only.id]
  iam_instance_profile   = aws_iam_instance_profile.instance.name

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
    echo 'export SIS_NOTIFY_SNS_TOPIC_ARN=${aws_sns_topic.alerts.arn}' > /etc/profile.d/sis-run.sh
    echo 'export ARTIFACTS_BUCKET=${aws_s3_bucket.artifacts.bucket}' >> /etc/profile.d/sis-run.sh
    echo 'export SIS_ARTIFACTS_BUCKET=${aws_s3_bucket.artifacts.bucket}' >> /etc/profile.d/sis-run.sh
    bash /home/ubuntu/omnibase/scripts/aws_bootstrap.sh
  EOT

  # A new repo_ref (every release bumps its default) must build a new box from
  # the new tag. Without this, a changed user_data only stops and starts the
  # same instance; cloud-init runs user_data on first boot only, so the box came
  # back on the old tag, its loop killed, and `apply` reported success
  # (OMNI-137). Replacing it loses runtime/ — sync the artifacts first
  # (docs/AWS_RUN.md, step 5d); open PRs are re-read from GitHub (OMNI-136).
  user_data_replace_on_change = true

  tags = { Name = var.name_prefix }

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

# --------------------------------------------------------------------------
# The pager (OMNI-62). A breaker trip, a spend-cap hit or a broken sandbox
# publishes here, and it reaches the same inbox as the budget alarm. The email
# subscription must be confirmed once (a link AWS mails after apply) — until
# then SNS accepts the publish and delivers nothing. The box finds the topic
# through /etc/profile.d/sis-run.sh, written by user_data above — which also
# names the artifacts bucket (ARTIFACTS_BUCKET, OMNI-124), so the runbook's
# sync step needs no placeholder filled in by hand.
# --------------------------------------------------------------------------

resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
}

resource "aws_sns_topic_subscription" "operator" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# --------------------------------------------------------------------------
# The infra-side brake. The LLM-side brake is SIS_BUDGET_USD, in the loop.
# --------------------------------------------------------------------------

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.monthly_budget_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "ACTUAL"
    threshold                  = 80
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alert_email]
  }

  notification {
    comparison_operator        = "GREATER_THAN"
    notification_type          = "ACTUAL"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    subscriber_email_addresses = [var.alert_email]
  }
}
