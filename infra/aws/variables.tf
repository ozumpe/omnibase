variable "region" {
  description = "Deliberately the same default as the engine's adapters.aws_region."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every resource name/tag in this stack."
  type        = string
  default     = "sis-first-run"
}

variable "instance_type" {
  description = <<-EOT
    4 vCPU / 16 GiB on purpose: the sandbox is capped at sandbox.cpus=2 and a
    cycle runs mypy --strict inside it repeatedly, Ray wants headroom beside
    that, and on a 2-vCPU box they all contend. ~$0.20/h on demand.
  EOT
  type        = string
  default     = "m7i.xlarge"
}

variable "root_volume_gb" {
  description = "gp3 root volume. Repo + poetry env + docker images fit in far less."
  type        = number
  default     = 40
}

variable "repo_url" {
  description = "Cloned by user_data at boot. Public repo, no credential needed."
  type        = string
  default     = "https://github.com/ozumpe/omnibase"
}

variable "repo_ref" {
  description = <<-EOT
    Release tag to check out on the box (OMNI-63). A tag, not a branch: a run's
    results are only worth something if they name the code that produced them,
    and a branch names whatever it pointed at when the box booted. A branch
    needs allow_branch_ref = true as well.
  EOT
  type        = string
  default     = "v0.3.1"
}

variable "allow_branch_ref" {
  description = "Deliberately run a branch (e.g. develop) instead of a release tag."
  type        = bool
  default     = false
}

variable "secret_name" {
  description = <<-EOT
    Name of the Secrets Manager secret the engine reads (SIS_AWS_SECRET_ID).
    Terraform creates only the shell; put the value in out-of-band:
      aws secretsmanager put-secret-value --secret-id <name> --secret-string file://...
    so it never enters Terraform state.
  EOT
  type        = string
  default     = "sis/first-run/credentials"
}

variable "monthly_budget_usd" {
  description = <<-EOT
    The infra-side spend alarm (the LLM side is the CEO brake, SIS_BUDGET_USD).
    A backstop against a forgotten instance, not a real-time kill: budget data
    lags hours.
  EOT
  type        = number
  default     = 25
}

variable "alert_email" {
  description = "Where the budget alarm and the loop's pages (SNS) go. Set it in terraform.tfvars (gitignored)."
  type        = string
}
