variable "name_prefix" {
  description = "The instance's Name tag; scripts/aws_scheduled_run.py finds the box by it."
  type        = string
}

variable "instance_type" {
  type = string
}

variable "root_volume_gb" {
  type = number
}

variable "repo_url" {
  type = string
}

variable "repo_ref" {
  description = "Release tag to check out on the box (OMNI-63)."
  type        = string
}

variable "allow_branch_ref" {
  type = bool
}

variable "security_group_id" {
  description = "The base stack's egress-only group."
  type        = string
}

variable "instance_profile" {
  description = "The base stack's instance profile name."
  type        = string
}

variable "alerts_topic_arn" {
  description = "The pager topic, exported on the box as SIS_NOTIFY_SNS_TOPIC_ARN."
  type        = string
}

variable "artifacts_bucket" {
  type = string
}

variable "extra_tags" {
  description = "Tags beside Name, e.g. who created the box."
  type        = map(string)
  default     = {}
}
