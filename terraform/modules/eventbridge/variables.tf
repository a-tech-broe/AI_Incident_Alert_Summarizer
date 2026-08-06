variable "name_prefix" {
  description = "Prefix for the bus, rule and archive names."
  type        = string
}

variable "target_lambda_arn" {
  description = "ARN of the Lambda the rule invokes."
  type        = string
}

variable "target_lambda_name" {
  description = "Name of the target Lambda, used for the invoke permission."
  type        = string
}

variable "event_sources" {
  description = "Values matched against the event `source` field."
  type        = list(string)
  default     = ["grafana.alerts"]

  validation {
    condition     = length(var.event_sources) > 0
    error_message = "event_sources must contain at least one value; an empty pattern would match every event on the bus."
  }
}

variable "event_detail_types" {
  description = "Values matched against `detail-type`. Empty matches any detail-type."
  type        = list(string)
  default     = []
}

variable "rule_enabled" {
  description = "Whether the rule is active. Disable to stop summarization without tearing down infrastructure."
  type        = bool
  default     = true
}

variable "max_retry_attempts" {
  description = "EventBridge delivery retries before dead-lettering."
  type        = number
  default     = 2
}

variable "max_event_age_seconds" {
  description = "Maximum age of an event EventBridge will still attempt to deliver."
  type        = number
  default     = 600
}

variable "dlq_arn" {
  description = "SQS queue ARN receiving undeliverable events."
  type        = string
}

variable "dlq_url" {
  description = "SQS queue URL. Needed to attach the queue policy that lets EventBridge write to it."
  type        = string
}

variable "enable_archive" {
  description = "Archive raw events for replay."
  type        = bool
  default     = true
}

variable "archive_retention_days" {
  description = "Archive retention in days. 0 retains indefinitely."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Tags applied to created resources."
  type        = map(string)
  default     = {}
}

variable "forward_cloudwatch_alarms" {
  description = "Republish CloudWatch alarm state changes from the account's default bus onto the alerts bus. Lets an existing workload feed the summarizer without application changes."
  type        = bool
  default     = false
}

variable "forwarded_alarm_name_prefixes" {
  description = "Only forward alarms whose name starts with one of these. Empty forwards every alarm in the account, which is rarely what you want when other projects share it."
  type        = list(string)
  default     = []
}
