variable "function_name" {
  description = "Lambda function name."
  type        = string
}

variable "description" {
  description = "Function description."
  type        = string
  default     = ""
}

variable "role_arn" {
  description = "Execution role ARN."
  type        = string
}

variable "handler" {
  description = "Handler entry point, module.function."
  type        = string
  default     = "app.lambda_handler"
}

variable "runtime" {
  description = "Lambda runtime identifier."
  type        = string
  default     = "python3.13"
}

variable "architecture" {
  description = "Instruction set architecture. arm64 is cheaper per GB-second."
  type        = string
  default     = "arm64"

  validation {
    condition     = contains(["x86_64", "arm64"], var.architecture)
    error_message = "architecture must be x86_64 or arm64."
  }
}

variable "memory_size" {
  description = "Memory in MB."
  type        = number
  default     = 512
}

variable "timeout" {
  description = "Timeout in seconds."
  type        = number
  default     = 60
}

variable "reserved_concurrent_executions" {
  description = "Reserved concurrency. -1 disables the reservation."
  type        = number
  default     = -1
}

variable "environment_variables" {
  description = "Environment variables passed to the handler."
  type        = map(string)
  default     = {}
}

variable "artifacts_bucket" {
  description = "S3 bucket holding the deployment package."
  type        = string
}

variable "log_group_name" {
  description = "Pre-created CloudWatch log group the function writes to."
  type        = string
}

variable "log_format" {
  description = "Lambda log format. Keep Text when the handler emits its own JSON: the JSON format re-wraps stdout, nesting the handler's fields one level deeper than the metric filters expect."
  type        = string
  default     = "Text"

  validation {
    condition     = contains(["Text", "JSON"], var.log_format)
    error_message = "log_format must be Text or JSON."
  }
}

variable "dead_letter_target_arn" {
  description = "SQS queue or SNS topic receiving invocations that exhausted their retries."
  type        = string
}

variable "async_retry_attempts" {
  description = "Retries for asynchronous invocations before dead-lettering."
  type        = number
  default     = 2
}

variable "async_max_event_age" {
  description = "Maximum age in seconds an async event may sit in the queue. Beyond this a summary is no longer actionable."
  type        = number
  default     = 600
}

variable "tracing_mode" {
  description = "X-Ray tracing mode."
  type        = string
  default     = "Active"

  validation {
    condition     = contains(["Active", "PassThrough"], var.tracing_mode)
    error_message = "tracing_mode must be Active or PassThrough."
  }
}

variable "enable_function_url" {
  description = "Create a Function URL for direct webhook ingestion."
  type        = bool
  default     = false
}

variable "function_url_auth_type" {
  description = "Function URL authorization. NONE relies on the handler's shared-secret check; AWS_IAM requires SigV4 callers."
  type        = string
  default     = "NONE"

  validation {
    condition     = contains(["NONE", "AWS_IAM"], var.function_url_auth_type)
    error_message = "function_url_auth_type must be NONE or AWS_IAM."
  }
}

variable "tags" {
  description = "Tags applied to the function."
  type        = map(string)
  default     = {}
}
