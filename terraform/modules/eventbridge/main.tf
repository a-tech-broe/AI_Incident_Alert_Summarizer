# A dedicated bus rather than the default one: alert traffic gets its own
# quota, its own archive, and rules here cannot accidentally match unrelated
# AWS service events.
resource "aws_cloudwatch_event_bus" "this" {
  name = "${var.name_prefix}-alerts"
  tags = var.tags
}

# Replay window for incident review — re-run a past alert through a new prompt
# without asking Grafana to re-fire it.
resource "aws_cloudwatch_event_archive" "this" {
  count = var.enable_archive ? 1 : 0

  name             = "${var.name_prefix}-alert-archive"
  event_source_arn = aws_cloudwatch_event_bus.this.arn
  retention_days   = var.archive_retention_days
  description      = "Retains raw alert events for replay and prompt regression testing."
}

locals {
  # detail-type is only included when the caller pins specific values; an empty
  # list must not become `"detail-type": []`, which matches nothing.
  event_pattern = merge(
    { source = var.event_sources },
    length(var.event_detail_types) > 0 ? { "detail-type" = var.event_detail_types } : {},
  )
}

resource "aws_cloudwatch_event_rule" "this" {
  name           = "${var.name_prefix}-alert-to-summarizer"
  description    = "Routes matching alert events to the AI incident summarizer."
  event_bus_name = aws_cloudwatch_event_bus.this.name
  event_pattern  = jsonencode(local.event_pattern)
  state          = var.rule_enabled ? "ENABLED" : "DISABLED"

  tags = var.tags
}

resource "aws_cloudwatch_event_target" "lambda" {
  rule           = aws_cloudwatch_event_rule.this.name
  event_bus_name = aws_cloudwatch_event_bus.this.name
  target_id      = "summarizer"
  arn            = var.target_lambda_arn

  retry_policy {
    maximum_retry_attempts       = var.max_retry_attempts
    maximum_event_age_in_seconds = var.max_event_age_seconds
  }

  dead_letter_config {
    arn = var.dlq_arn
  }
}

resource "aws_lambda_permission" "eventbridge" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = var.target_lambda_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.this.arn
}

# EventBridge writes to the DLQ under its own principal, so the queue policy —
# not the Lambda's role — is what authorizes it.
data "aws_iam_policy_document" "dlq" {
  statement {
    sid    = "AllowEventBridgeDeadLetter"
    effect = "Allow"

    principals {
      type        = "Service"
      identifiers = ["events.amazonaws.com"]
    }

    actions   = ["sqs:SendMessage"]
    resources = [var.dlq_arn]

    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [aws_cloudwatch_event_rule.this.arn]
    }
  }
}

resource "aws_sqs_queue_policy" "dlq" {
  queue_url = var.dlq_url
  policy    = data.aws_iam_policy_document.dlq.json
}
