variable "name_prefix" {
  description = "Prefix for alarm, dashboard and metric names."
  type        = string
}

variable "function_name" {
  description = "Lambda function name. Determines the log group path and alarm dimensions."
  type        = string
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention in days."
  type        = number
  default     = 30
}

variable "kms_key_arn" {
  description = "Customer-managed KMS key for log group encryption. Null uses the CloudWatch Logs service key."
  type        = string
  default     = null
}

variable "alarm_emails" {
  description = "Email addresses subscribed to the alarm topic."
  type        = list(string)
  default     = []
}

variable "dlq_queue_name" {
  description = "Name of the dead-letter queue to alarm on."
  type        = string
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds. The duration alarm threshold is derived from it."
  type        = number
  default     = 60
}

variable "tags" {
  description = "Tags applied to created resources."
  type        = map(string)
  default     = {}
}
