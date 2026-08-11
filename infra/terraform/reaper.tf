# THE ACTUAL COST GUARD. Read budget.tf first for why this file has to exist.
#
# AWS has no hard spending cap. Budgets notify; they do not stop. The only
# native enforcement is a Budget Action, and all three of its forms are either
# unusable or wrong here:
#
#   scp_action_definition  -- this account is the MANAGEMENT account of its
#                             organization, and service control policies never
#                             apply to the management account.
#   ssm_action_definition  -- requires explicit instance_ids, and EKS nodes get
#                             unpredictable ids from an autoscaling group.
#   iam_action_definition  -- would work, but only stops NEW resources being
#                             created. It cannot delete a control plane that is
#                             already billing, which is the exact failure this
#                             project has. It also risks denying unrelated live
#                             workloads in this account if scoped loosely.
#
# So the cap is enforced by deletion on a timer instead of by policy, and it
# targets the thing that actually leaks: a cluster nobody remembered to tear
# down. Exposure becomes TTL + one schedule interval, about 9 hours or $0.90,
# instead of unbounded.
#
# Running cost of the guard itself is effectively zero: ~720 Lambda invocations
# a month at 128 MB for a second or two each, well inside the perpetual free
# tier, plus a log group with a 7-day retention so it cannot grow without bound.

data "aws_caller_identity" "current" {}

# Zipped from source at plan time rather than committed as a binary: a .zip in
# a public repo is an unreviewable blob, and the point of this file is that a
# reader can see exactly what gets permission to delete clusters.
data "archive_file" "reaper" {
  type        = "zip"
  source_file = "${path.module}/lambda/reaper.py"
  output_path = "${path.module}/.terraform/reaper.zip"
}

resource "aws_iam_role" "reaper" {
  name = "${var.project}-reaper"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# SCOPED TO ONE REGION ON PURPOSE. This account carries unrelated production
# workloads; the reaper is physically incapable of deleting an EKS cluster
# outside var.aws_region no matter what the code does. The tag gate inside
# reaper.py is the second, independent check -- neither is trusted alone.
resource "aws_iam_role_policy" "reaper" {
  name = "${var.project}-reaper"
  role = aws_iam_role.reaper.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # ListClusters takes no resource qualifier; the region is pinned by the
        # client's own endpoint, and DescribeCluster below is what gates action.
        Effect   = "Allow"
        Action   = ["eks:ListClusters"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = [
          "eks:DescribeCluster",
          "eks:ListNodegroups",
          "eks:DeleteCluster",
        ]
        Resource = "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:cluster/*"
      },
      {
        Effect   = "Allow"
        Action   = ["eks:DescribeNodegroup", "eks:DeleteNodegroup"]
        Resource = "arn:aws:eks:${var.aws_region}:${data.aws_caller_identity.current.account_id}:nodegroup/*/*/*"
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.reaper.arn}:*"
      },
    ]
  })
}

# Declared rather than left to Lambda's implicit creation, which defaults to
# never-expire. A guard that silently accrues log storage forever would be its
# own small version of the bug it exists to prevent.
resource "aws_cloudwatch_log_group" "reaper" {
  name              = "/aws/lambda/${var.project}-reaper"
  retention_in_days = 7
}

resource "aws_lambda_function" "reaper" {
  function_name = "${var.project}-reaper"
  role          = aws_iam_role.reaper.arn
  handler       = "reaper.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 128

  filename         = data.archive_file.reaper.output_path
  source_code_hash = data.archive_file.reaper.output_base64sha256

  environment {
    variables = {
      TTL_HOURS = tostring(var.cluster_ttl_hours)
      TAG_KEY   = "lifecycle"
      TAG_VALUE = "ephemeral"
    }
  }

  depends_on = [aws_cloudwatch_log_group.reaper]
}

resource "aws_iam_role" "reaper_scheduler" {
  name = "${var.project}-reaper-scheduler"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "scheduler.amazonaws.com" }
      Action    = "sts:AssumeRole"
      # Without this the role is assumable by any EventBridge Scheduler in any
      # account -- the confused-deputy shape AWS documents for service principals.
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

resource "aws_iam_role_policy" "reaper_scheduler" {
  name = "${var.project}-reaper-scheduler"
  role = aws_iam_role.reaper_scheduler.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "lambda:InvokeFunction"
      Resource = aws_lambda_function.reaper.arn
    }]
  })
}

# Hourly, not nightly. A nightly sweep means a cluster created at 06:00 bills
# for a full day before anything looks at it; hourly makes worst-case exposure
# TTL plus one hour. EventBridge Scheduler rather than an EventBridge rule:
# it needs no resource-based policy on the function and no separate
# aws_lambda_permission.
resource "aws_scheduler_schedule" "reaper" {
  name                = "${var.project}-reaper"
  schedule_expression = "rate(1 hour)"

  # OFF, not a window. There is no thundering-herd concern with one target and
  # a fixed start time makes the log timestamps readable.
  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = aws_lambda_function.reaper.arn
    role_arn = aws_iam_role.reaper_scheduler.arn
  }
}

output "reaper_note" {
  description = "How to keep a cluster alive past the TTL without touching terraform."
  value       = "Clusters tagged lifecycle=ephemeral are deleted after ${var.cluster_ttl_hours}h. To exempt one: aws eks untag-resource --resource-arn <cluster-arn> --tag-keys lifecycle"
}
