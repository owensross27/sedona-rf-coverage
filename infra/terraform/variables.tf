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

# The outer backstop, deliberately ABOVE the realistic worst case rather than
# near it. A forgotten control plane for a fortnight is ~$34, which is under
# this number and would never trip it -- that scenario is handled by the reaper
# in reaper.tf, not here. This one exists for the failures nobody modelled: a
# NAT gateway that reappears, an on-demand instance type slipping into the
# nodegroup list, a job that never terminates.
variable "hard_cap_usd" {
  description = "Outer backstop budget for var.aws_region. Notification only -- AWS has no native hard cap; see reaper.tf for what actually enforces."
  type        = number
  default     = 45

  # The meaningful check -- that this exceeds budget_limit_usd -- cannot live
  # here: Terraform 1.5.7 restricts a validation block to its own variable
  # (cross-variable references arrived in 1.9). It is a precondition on
  # aws_budgets_budget.hard_cap instead, which fails at the same point, plan.
  validation {
    condition     = var.hard_cap_usd > 0
    error_message = "hard_cap_usd must be positive."
  }
}

# 8 hours comfortably covers the 4h EKS spike timebox and a ~6h statewide
# session, so the reaper never interrupts planned work. Anything still running
# past 8 hours is, by this project's own plan, forgotten.
variable "cluster_ttl_hours" {
  description = "Age after which a cluster tagged lifecycle=ephemeral is deleted by the reaper. Exempt a long run by removing the tag, not by changing this."
  type        = number
  default     = 8

  validation {
    condition     = var.cluster_ttl_hours >= 1
    error_message = "cluster_ttl_hours below 1 would race cluster creation, which takes ~20 minutes."
  }
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
