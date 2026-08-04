variable "name_prefix" {
  description = "Prefix for the role name."
  type        = string
}

variable "function_name" {
  description = "Lambda function name the role is built for. Documentation only; scoping is done via the log group ARN."
  type        = string
}

variable "log_group_arn" {
  description = "ARN of the Lambda's CloudWatch log group."
  type        = string
}

variable "artifacts_bucket_arn" {
  description = "ARN of the S3 bucket holding deployment packages."
  type        = string
}

variable "secret_arns" {
  description = "Secrets Manager ARNs the Lambda may read, keyed by integration name."
  type        = map(string)
}

variable "secrets_kms_key_arn" {
  description = "Customer-managed KMS key encrypting the secrets. Null when using the AWS-managed key, which needs no explicit grant."
  type        = string
  default     = null
}

variable "bedrock_invoke_arns" {
  description = "Bedrock resource ARNs the role may invoke."
  type        = list(string)
}

variable "bedrock_guardrail_arn" {
  description = "Guardrail ARN to grant ApplyGuardrail on. Null when guardrails are disabled."
  type        = string
  default     = null
}

variable "ecs_cluster_names" {
  description = "ECS clusters the Lambda may describe. Empty grants account-wide describe access."
  type        = list(string)
  default     = []
}

variable "dlq_arn" {
  description = "ARN of the dead-letter queue the Lambda may write to."
  type        = string
}

variable "permissions_boundary_arn" {
  description = "Optional permissions boundary applied to the execution role."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags applied to the role."
  type        = map(string)
  default     = {}
}
