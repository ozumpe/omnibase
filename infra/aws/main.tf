# One instance, one secret, one bucket, one budget. Design: docs/AWS_RUN.md.
# The instance itself is modules/box; box/ applies it alone (OMNI-169).

data "aws_vpc" "default" {
  default = true
}

data "aws_caller_identity" "current" {}

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
# The box. Its definition is the module in modules/box, shared with the
# box-only stack in box/ that a scheduled run applies and destroys (OMNI-169).
# With create_instance = false this stack is the long-lived base only: the
# secret keeps its value and the pager its confirmed subscription between
# scheduled runs, and no second box carries the same Name tag.
# --------------------------------------------------------------------------

module "box" {
  source = "./modules/box"
  count  = var.create_instance ? 1 : 0

  name_prefix       = var.name_prefix
  instance_type     = var.instance_type
  root_volume_gb    = var.root_volume_gb
  repo_url          = var.repo_url
  repo_ref          = var.repo_ref
  allow_branch_ref  = var.allow_branch_ref
  security_group_id = aws_security_group.egress_only.id
  instance_profile  = aws_iam_instance_profile.instance.name
  alerts_topic_arn  = aws_sns_topic.alerts.arn
  artifacts_bucket  = aws_s3_bucket.artifacts.bucket
}

moved {
  from = aws_instance.sis
  to   = module.box[0].aws_instance.sis
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
