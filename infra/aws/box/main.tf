# The base is found by name, never by its state file, which stays on the
# laptop of whoever applied it. Every name here is the one infra/aws gives.

data "aws_caller_identity" "current" {}

data "aws_security_group" "egress_only" {
  name = "${var.name_prefix}-egress-only"
}

locals {
  account = data.aws_caller_identity.current.account_id
}

module "box" {
  source = "../modules/box"

  name_prefix       = var.name_prefix
  instance_type     = var.instance_type
  root_volume_gb    = var.root_volume_gb
  repo_url          = var.repo_url
  repo_ref          = var.repo_ref
  allow_branch_ref  = var.allow_branch_ref
  security_group_id = data.aws_security_group.egress_only.id
  instance_profile  = "${var.name_prefix}-instance"
  alerts_topic_arn  = "arn:aws:sns:${var.region}:${local.account}:${var.name_prefix}-alerts"
  artifacts_bucket  = "${var.name_prefix}-artifacts-${local.account}"
  extra_tags = {
    "sis:created-by" = "scheduled-run"
    "sis:run-id"     = var.run_id
  }
}
