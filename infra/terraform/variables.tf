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

# RAISED 20 -> 35 on 2026-08-11, and the reason matters more than the number.
# This budget filters on REGION, and us-west-2 is not exclusive to this project:
# the sibling repo ~/s2-field-ndvi billed $13.80 here on 08-08/09, taking a $20
# limit to 74% before this project's first cluster existed. The 50% ACTUAL alert
# therefore fired for spend that was not ours, and the remaining headroom was
# ~$5 against a statewide run modelled at $2-4.
#
# An alert that mostly reports another project's spend gets ignored, which is
# the failure this raise prevents. $35 covers the sibling's historical $13.80
# (finished and torn down, so it will not grow) plus this project's own ~$7
# projection with room to spare, and still sits below the $45 backstop.
#
# This project alone remains a ~$7 job: see README "Cost". If the sibling repo
# runs here again, re-check this number rather than assuming it still holds.
variable "budget_limit_usd" {
  description = "Monthly cost budget for var.aws_region. NOTE the region is shared with the sibling s2-field-ndvi project, so this is sized for the region, not for this repo alone."
  type        = number
  default     = 35
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
