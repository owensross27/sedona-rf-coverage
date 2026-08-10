variable "project" {
  description = "Name prefix for every resource in this module."
  type        = string
  default     = "rf-coverage"
}

variable "aws_region" {
  description = "Must match the Makefile's AWS_REGION and infra/eks/cluster.yaml's metadata.region. us-west-2 is where every open input bucket lives (overturemaps-us-west-2, ookla-open-data, prd-tnm), which is what makes S3-to-EC2 transfer free."
  type        = string
  default     = "us-west-2"
}

variable "ecr_repo" {
  description = "Must match the Makefile's ECR_REPO."
  type        = string
  default     = "sedona-rf-coverage"
}

variable "image_retain_count" {
  description = "Images kept in ECR. Each is ~3.8 GB of layers at $0.10/GB-month."
  type        = number
  default     = 3
}

variable "budget_limit_usd" {
  description = "Monthly cost budget for var.aws_region. The statewide run is modelled at ~$21, worst case ~$35. $20 alerts before that, not after."
  type        = number
  default     = 20
}

# NO DEFAULT, ON PURPOSE. A budget with no reachable subscriber is worse than
# no budget at all: it reads as a guardrail in a public repo while notifying
# nobody. With no default, `terraform plan` stops and asks. With a placeholder
# default it would apply clean and stay silent while a control plane bills.
variable "budget_alert_email" {
  description = "Email for budget alerts. Notifications come from no-reply@budgets.amazonaws.com -- check spam once, after the first apply."
  type        = string

  # The missing default catches an empty value. This catches "changeme".
  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.budget_alert_email))
    error_message = "budget_alert_email must be a real address; budget notifications have no other delivery path."
  }
}
