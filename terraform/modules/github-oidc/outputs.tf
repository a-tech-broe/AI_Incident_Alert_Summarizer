output "role_arn" {
  description = "ARN of the role GitHub Actions assumes. Set as the AWS_ROLE_ARN repository variable."
  value       = aws_iam_role.github_actions.arn
}

output "role_name" {
  description = "Name of the deployment role."
  value       = aws_iam_role.github_actions.name
}

output "oidc_provider_arn" {
  description = "ARN of the GitHub OIDC provider in use."
  value       = local.oidc_provider_arn
}

output "allowed_subjects" {
  description = "Exact `sub` claims the trust policy accepts. Useful when debugging AssumeRoleWithWebIdentity denials."
  value       = local.allowed_subjects
}
