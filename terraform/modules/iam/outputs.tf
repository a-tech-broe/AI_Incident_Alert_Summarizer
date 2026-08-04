output "role_arn" {
  description = "Execution role ARN."
  value       = aws_iam_role.lambda.arn
}

output "role_name" {
  description = "Execution role name."
  value       = aws_iam_role.lambda.name
}

output "role_id" {
  description = "Execution role ID."
  value       = aws_iam_role.lambda.id
}
