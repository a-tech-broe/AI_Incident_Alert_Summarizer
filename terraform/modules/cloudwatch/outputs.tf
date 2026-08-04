output "log_group_name" {
  description = "Lambda log group name."
  value       = aws_cloudwatch_log_group.lambda.name
}

output "log_group_arn" {
  description = "Lambda log group ARN."
  value       = aws_cloudwatch_log_group.lambda.arn
}

output "alarm_topic_arn" {
  description = "SNS topic alarms publish to."
  value       = aws_sns_topic.alarms.arn
}

output "metric_namespace" {
  description = "Custom metric namespace populated by the log metric filters."
  value       = local.metric_namespace
}

output "dashboard_name" {
  description = "CloudWatch dashboard name."
  value       = aws_cloudwatch_dashboard.this.dashboard_name
}
