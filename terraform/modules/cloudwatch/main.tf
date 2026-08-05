resource "aws_cloudwatch_log_group" "lambda" {
  name              = "/aws/lambda/${var.function_name}"
  retention_in_days = var.log_retention_days
  kms_key_id        = var.kms_key_arn

  tags = var.tags
}

# ---------------------------------------------------------------------------
# Metric filters
#
# The handler emits single-line JSON. These turn its own events into metrics so
# alarms can fire on summarizer-specific failures, not just Lambda errors.
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "handler_errors" {
  name           = "${var.name_prefix}-handler-errors"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ $.level = \"ERROR\" }"

  metric_transformation {
    name          = "HandlerErrors"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "bedrock_failures" {
  name           = "${var.name_prefix}-bedrock-failures"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ $.event = \"bedrock_invoke_failed\" }"

  metric_transformation {
    name          = "BedrockInvocationFailures"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

resource "aws_cloudwatch_log_metric_filter" "slack_failures" {
  name           = "${var.name_prefix}-slack-failures"
  log_group_name = aws_cloudwatch_log_group.lambda.name
  pattern        = "{ $.event = \"slack_post_failed\" }"

  metric_transformation {
    name          = "SlackDeliveryFailures"
    namespace     = local.metric_namespace
    value         = "1"
    default_value = "0"
    unit          = "Count"
  }
}

# ---------------------------------------------------------------------------
# Alarm routing
# ---------------------------------------------------------------------------

resource "aws_sns_topic" "alarms" {
  name              = "${var.name_prefix}-alarms"
  kms_master_key_id = "alias/aws/sns"

  tags = var.tags
}

resource "aws_sns_topic_subscription" "email" {
  for_each = toset(var.alarm_emails)

  topic_arn = aws_sns_topic.alarms.arn
  protocol  = "email"
  endpoint  = each.value
}

locals {
  metric_namespace = "${var.name_prefix}/Summarizer"
  alarm_actions    = [aws_sns_topic.alarms.arn]
}

# ---------------------------------------------------------------------------
# Alarms
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  alarm_name          = "${var.name_prefix}-lambda-errors"
  alarm_description   = "Summarizer Lambda raised unhandled errors. Incoming alerts may not be reaching Slack."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { FunctionName = var.function_name }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "lambda_throttles" {
  alarm_name          = "${var.name_prefix}-lambda-throttles"
  alarm_description   = "Summarizer Lambda is being throttled. Reserved concurrency may be too low for the current alert volume."
  namespace           = "AWS/Lambda"
  metric_name         = "Throttles"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 1
  comparison_operator = "GreaterThanOrEqualToThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { FunctionName = var.function_name }

  alarm_actions = local.alarm_actions

  tags = var.tags
}

# Duration is the leading indicator for timeouts: once p99 crosses 80% of the
# configured timeout, enrichment or Bedrock latency is about to start dropping
# summaries.
resource "aws_cloudwatch_metric_alarm" "lambda_duration" {
  alarm_name          = "${var.name_prefix}-lambda-duration-p99"
  alarm_description   = "p99 duration exceeded 80% of the configured timeout."
  namespace           = "AWS/Lambda"
  metric_name         = "Duration"
  extended_statistic  = "p99"
  period              = 300
  evaluation_periods  = 2
  threshold           = var.lambda_timeout * 1000 * 0.8
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { FunctionName = var.function_name }

  alarm_actions = local.alarm_actions

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  alarm_name          = "${var.name_prefix}-dlq-not-empty"
  alarm_description   = "Alerts landed in the dead-letter queue and were never summarized."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = { QueueName = var.dlq_queue_name }

  alarm_actions = local.alarm_actions
  ok_actions    = local.alarm_actions

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "bedrock_failures" {
  alarm_name          = "${var.name_prefix}-bedrock-failures"
  alarm_description   = "Bedrock invocations are failing. Check model access, throttling and the inference profile ID."
  namespace           = local.metric_namespace
  metric_name         = aws_cloudwatch_log_metric_filter.bedrock_failures.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 2
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions

  tags = var.tags
}

resource "aws_cloudwatch_metric_alarm" "slack_failures" {
  alarm_name          = "${var.name_prefix}-slack-delivery-failures"
  alarm_description   = "Summaries were generated but could not be delivered to Slack."
  namespace           = local.metric_namespace
  metric_name         = aws_cloudwatch_log_metric_filter.slack_failures.metric_transformation[0].name
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = local.alarm_actions

  tags = var.tags
}

# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

resource "aws_cloudwatch_dashboard" "this" {
  dashboard_name = "${var.name_prefix}-overview"

  dashboard_body = jsonencode({
    widgets = [
      {
        type   = "metric"
        x      = 0
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Invocations and errors"
          region = data.aws_region.current.name
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            ["AWS/Lambda", "Invocations", "FunctionName", var.function_name],
            [".", "Errors", ".", "."],
            [".", "Throttles", ".", "."],
          ]
        }
      },
      {
        type   = "metric"
        x      = 12
        y      = 0
        width  = 12
        height = 6
        properties = {
          title  = "Duration"
          region = data.aws_region.current.name
          view   = "timeSeries"
          period = 300
          metrics = [
            ["AWS/Lambda", "Duration", "FunctionName", var.function_name, { stat = "Average" }],
            ["...", { stat = "p99" }],
          ]
          annotations = {
            horizontal = [{ label = "timeout", value = var.lambda_timeout * 1000 }]
          }
        }
      },
      {
        type   = "metric"
        x      = 0
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Pipeline failures"
          region = data.aws_region.current.name
          view   = "timeSeries"
          stat   = "Sum"
          period = 300
          metrics = [
            [local.metric_namespace, "BedrockInvocationFailures"],
            [".", "SlackDeliveryFailures"],
            [".", "HandlerErrors"],
          ]
        }
      },
      {
        type   = "log"
        x      = 12
        y      = 6
        width  = 12
        height = 6
        properties = {
          title  = "Recent errors"
          region = data.aws_region.current.name
          query  = "SOURCE '${aws_cloudwatch_log_group.lambda.name}' | fields @timestamp, event, message, alert_name | filter level = 'ERROR' | sort @timestamp desc | limit 20"
        }
      },
    ]
  })
}

data "aws_region" "current" {}
