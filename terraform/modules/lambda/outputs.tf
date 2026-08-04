output "function_name" {
  description = "Lambda function name."
  value       = aws_lambda_function.this.function_name
}

output "function_arn" {
  description = "Unqualified function ARN."
  value       = aws_lambda_function.this.arn
}

output "qualified_arn" {
  description = "Version-qualified function ARN."
  value       = aws_lambda_function.this.qualified_arn
}

output "alias_arn" {
  description = "ARN of the `live` alias."
  value       = aws_lambda_alias.live.arn
}

output "function_url" {
  description = "Function URL endpoint, or null when disabled."
  value       = try(aws_lambda_function_url.this[0].function_url, null)
}

output "s3_key" {
  description = "S3 key the deployment package is expected at."
  value       = local.s3_key
}
