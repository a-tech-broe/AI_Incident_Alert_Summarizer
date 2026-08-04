variable "name_prefix" {
  description = "Prefix for secret paths, e.g. ai-incident-summarizer-dev."
  type        = string
}

variable "secrets" {
  description = "Secrets to create, keyed by integration name."
  type = map(object({
    description = string
  }))
}

variable "kms_key_id" {
  description = "Customer-managed KMS key for the secrets. Null uses the aws/secretsmanager managed key."
  type        = string
  default     = null
}

variable "recovery_window_in_days" {
  description = "Days a deleted secret is recoverable. 0 forces immediate deletion, which keeps non-prod environments re-creatable."
  type        = number
  default     = 0
}

variable "tags" {
  description = "Tags applied to every secret."
  type        = map(string)
  default     = {}
}
