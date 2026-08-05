output "lambda_function_name" {
  description = "Name of the summarizer Lambda. The deploy workflow uses this for update-function-code."
  value       = module.lambda.function_name
}

output "lambda_function_arn" {
  description = "ARN of the summarizer Lambda."
  value       = module.lambda.function_arn
}

output "lambda_function_url" {
  description = "HTTPS endpoint for Grafana's webhook contact point. Null when the function URL is disabled."
  value       = module.lambda.function_url
}

output "artifacts_bucket" {
  description = "S3 bucket holding Lambda deployment packages."
  value       = module.artifacts.bucket_id
}

output "artifacts_bucket_key" {
  description = "S3 key the deploy workflow uploads the deployment package to."
  value       = module.lambda.s3_key
}

output "event_bus_name" {
  description = "Custom EventBridge bus that alert producers publish to."
  value       = module.eventbridge.event_bus_name
}

output "event_rule_name" {
  description = "EventBridge rule routing matched alerts to the Lambda."
  value       = module.eventbridge.rule_name
}

output "dead_letter_queue_url" {
  description = "SQS queue holding alerts that EventBridge could not deliver."
  value       = aws_sqs_queue.dlq.url
}

output "log_group_name" {
  description = "CloudWatch log group for the Lambda."
  value       = module.observability.log_group_name
}

output "alarm_topic_arn" {
  description = "SNS topic that operational alarms publish to."
  value       = module.observability.alarm_topic_arn
}

output "secret_names" {
  description = "Secrets Manager secret names, keyed by integration. Populate these out of band before the first real alert."
  value       = module.secrets.secret_names
}

output "bedrock_model_id" {
  description = "Bedrock model or inference profile used for summarization."
  value       = module.bedrock.model_id
}

output "github_actions_role_arn" {
  description = "IAM role GitHub Actions assumes via OIDC. Set this as the AWS_ROLE_ARN repository variable."
  value       = try(module.github_oidc[0].role_arn, null)
}
