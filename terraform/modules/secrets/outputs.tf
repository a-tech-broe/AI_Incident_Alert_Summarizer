output "secret_arns" {
  description = "Secret ARNs keyed by integration name."
  value       = { for k, v in aws_secretsmanager_secret.this : k => v.arn }
}

output "secret_names" {
  description = "Secret names keyed by integration name. Passed to the Lambda as environment variables."
  value       = { for k, v in aws_secretsmanager_secret.this : k => v.name }
}

output "secret_arn_list" {
  description = "Flat list of secret ARNs, for IAM resource blocks."
  value       = [for v in aws_secretsmanager_secret.this : v.arn]
}
