output "model_id" {
  description = "Model or inference profile ID passed to the Lambda."
  value       = var.model_id
}

output "invoke_resource_arns" {
  description = "ARNs the execution role needs bedrock:InvokeModel on. Covers both the inference profile and the foundation models behind it."
  value       = local.invoke_resource_arns
}

output "guardrail_id" {
  description = "Guardrail ID, or an empty string when guardrails are disabled."
  value       = try(aws_bedrock_guardrail.this[0].guardrail_id, "")
}

output "guardrail_arn" {
  description = "Guardrail ARN, or null when guardrails are disabled."
  value       = try(aws_bedrock_guardrail.this[0].guardrail_arn, null)
}

output "guardrail_version" {
  description = "Published guardrail version, or an empty string when guardrails are disabled."
  value       = try(aws_bedrock_guardrail_version.this[0].version, "")
}
