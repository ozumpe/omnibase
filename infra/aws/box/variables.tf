# Defaults match infra/aws/variables.tf; the box must be built the same way
# whichever stack builds it.

variable "region" {
  type    = string
  default = "us-east-1"
}

variable "name_prefix" {
  description = "The base stack's name_prefix: the names below are derived from it."
  type        = string
  default     = "sis-first-run"
}

variable "instance_type" {
  type    = string
  default = "m7i.xlarge"
}

variable "root_volume_gb" {
  type    = number
  default = 40
}

variable "repo_url" {
  type    = string
  default = "https://github.com/ozumpe/omnibase"
}

variable "repo_ref" {
  description = "Release tag to check out on the box (OMNI-63); a branch needs allow_branch_ref."
  type        = string
  default     = "v0.5.0"
}

variable "allow_branch_ref" {
  type    = bool
  default = false
}

variable "run_id" {
  description = "The scheduled run that created the box, as a tag."
  type        = string
  default     = ""
}
