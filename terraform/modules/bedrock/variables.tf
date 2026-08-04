variable "name_prefix" {
  description = "Prefix for the guardrail name."
  type        = string
}

variable "model_id" {
  description = "Bedrock model or cross-region inference profile ID."
  type        = string
}

variable "enable_guardrail" {
  description = "Create a guardrail and apply it to every invocation."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Tags applied to created resources."
  type        = map(string)
  default     = {}
}
