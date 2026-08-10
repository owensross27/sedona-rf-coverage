# WRITTEN FRESH, NOT PORTED. ~/geospatial-sms/infrastructure/terraform/
# billing.tf is a weekly SMS-report Lambda -- EventBridge cron to Lambda to SMS
# -- and there is no aws_budgets_budget anywhere in that module. Nothing to
# copy; this is the one file here with no port lineage.
#
# SCOPED TO THE REGION, NOT THE ACCOUNT, and that is the whole design decision.
# This AWS account is not empty: it carries CloudFront distributions, Route53
# hosted zones and buckets belonging to unrelated live sites. An unfiltered $20
# monthly budget would alarm on those and say nothing useful about this
# project.
#
# us-west-2 is the correct filter because "nothing of mine should exist in
# us-west-2" is ALREADY this project's teardown assertion -- see
# infra/eks/cluster.yaml's metadata.region comment. It captures the entire cost
# risk: EKS control plane, EC2 spot, EBS, this bucket, this ECR repo.
#
# Region is a native Cost Explorer dimension, so it needs no setup. A tag
# filter would have needed `project` activated as a cost allocation tag in
# Billing first, which takes up to 24 h to take effect and is not retroactive.
# This budget has to be live BEFORE the first cluster, not a day after it.
#
# KNOWN GAP: CloudFront and Route53 bill as global services under us-east-1 and
# fall outside this filter. Both are deferred (see main.tf). Widen the filter
# or add a second budget on the day the serving leg is built.

resource "aws_budgets_budget" "rf" {
  name         = "${var.project}-monthly"
  budget_type  = "COST"
  limit_amount = tostring(var.budget_limit_usd)
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  cost_filter {
    name   = "Region"
    values = [var.aws_region]
  }

  # Actual spend at 50/80/100%, with the caveat stated where it matters: AWS
  # refreshes billing data roughly three times a day, so an ACTUAL alert can
  # lag real spend by 8-12 hours. A forgotten control plane burns ~$2.40/day,
  # so the 50% ACTUAL alert could arrive four days in. This is a backstop, not
  # a stop button; the real-time guard is `make status` and `make check-nat`.
  dynamic "notification" {
    for_each = [50, 80, 100]

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.budget_alert_email]
    }
  }

  # The one that actually catches a forgotten cluster. A forecast crosses the
  # limit within hours of the meter starting rather than days: ~$0.54/hr
  # forecasts to roughly $390 for the month the moment nodes come up.
  notification {
    comparison_operator        = "GREATER_THAN"
    threshold                  = 100
    threshold_type             = "PERCENTAGE"
    notification_type          = "FORECASTED"
    subscriber_email_addresses = [var.budget_alert_email]
  }
}
