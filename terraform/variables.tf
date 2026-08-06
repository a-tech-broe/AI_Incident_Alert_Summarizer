variable "project_name" {
  description = "Short project identifier used as the prefix for every resource name."
  type        = string
  default     = "ai-incident-summarizer"

  validation {
    condition     = can(regex("^[a-z0-9-]{3,32}$", var.project_name))
    error_message = "project_name must be 3-32 characters of lowercase letters, digits or hyphens."
  }
}

variable "environment" {
  description = "Deployment environment. Drives naming and tagging."
  type        = string
  default     = "dev"

  validation {
    condition     = contains(["dev", "staging", "prod"], var.environment)
    error_message = "environment must be one of: dev, staging, prod."
  }
}

variable "aws_region" {
  description = "AWS region for all regional resources."
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = "Additional tags merged into the default tag set."
  type        = map(string)
  default     = {}
}

# ---------------------------------------------------------------------------
# Lambda
# ---------------------------------------------------------------------------

variable "lambda_runtime" {
  description = "Python runtime for the summarizer Lambda."
  type        = string
  default     = "python3.13"
}

variable "lambda_memory_size" {
  description = "Lambda memory in MB. Also scales CPU allocation."
  type        = number
  default     = 512

  validation {
    condition     = var.lambda_memory_size >= 128 && var.lambda_memory_size <= 10240
    error_message = "lambda_memory_size must be between 128 and 10240 MB."
  }
}

variable "lambda_timeout" {
  description = "Lambda timeout in seconds. Must exceed the Bedrock call budget."
  type        = number
  default     = 60

  validation {
    condition     = var.lambda_timeout >= 10 && var.lambda_timeout <= 900
    error_message = "lambda_timeout must be between 10 and 900 seconds."
  }
}

variable "lambda_reserved_concurrency" {
  description = "Reserved concurrent executions. -1 disables the reservation. Bounds Bedrock spend during an alert storm."
  type        = number
  default     = 10
}

variable "lambda_log_level" {
  description = "Log level for the Lambda handler."
  type        = string
  default     = "INFO"

  validation {
    condition     = contains(["DEBUG", "INFO", "WARNING", "ERROR"], var.lambda_log_level)
    error_message = "lambda_log_level must be DEBUG, INFO, WARNING or ERROR."
  }
}

variable "enable_function_url" {
  description = "Expose a Lambda Function URL. Required when Grafana posts webhooks directly to Lambda rather than through EventBridge."
  type        = bool
  default     = true
}

# ---------------------------------------------------------------------------
# Observability
# ---------------------------------------------------------------------------

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the Lambda log group."
  type        = number
  default     = 30

  validation {
    condition = contains(
      [0, 1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365, 400, 545, 731, 1096, 1827, 2192, 2557, 2922, 3288, 3653],
      var.log_retention_days
    )
    error_message = "log_retention_days must be a retention period CloudWatch Logs accepts."
  }
}

variable "alarm_email_subscriptions" {
  description = "Email addresses subscribed to the operational alarm topic. Each requires manual confirmation."
  type        = list(string)
  default     = []
}

# ---------------------------------------------------------------------------
# Bedrock
# ---------------------------------------------------------------------------

variable "bedrock_model_id" {
  description = "Bedrock model or inference profile ID used for summarization. Cross-region inference profiles are prefixed with the geography (us./eu./apac.)."
  type        = string
  default     = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
}

variable "bedrock_max_tokens" {
  description = "Maximum tokens the model may generate per summary."
  type        = number
  default     = 2000
}

variable "bedrock_temperature" {
  description = "Sampling temperature. Keep low so summaries stay grounded in the supplied telemetry."
  type        = number
  default     = 0.2
}

variable "enable_bedrock_guardrail" {
  description = "Create a Bedrock guardrail and apply it to every invocation. Blocks prompt-injection style content arriving from alert payloads."
  type        = bool
  default     = false
}

# ---------------------------------------------------------------------------
# Alert ingestion
# ---------------------------------------------------------------------------

variable "event_sources" {
  description = "EventBridge `source` values the summarizer rule matches."
  type        = list(string)
  default     = ["grafana.alerts"]
}

variable "event_detail_types" {
  description = "EventBridge `detail-type` values to match. Empty matches any detail-type for the configured sources."
  type        = list(string)
  default     = []
}

variable "event_max_retry_attempts" {
  description = "EventBridge retries before an event is sent to the dead-letter queue."
  type        = number
  default     = 2
}

# ---------------------------------------------------------------------------
# Downstream integrations
# ---------------------------------------------------------------------------

variable "forward_cloudwatch_alarms" {
  description = "Republish CloudWatch alarm state changes onto the alerts bus, so an existing EC2/ALB/RDS workload can feed the summarizer without application changes."
  type        = bool
  default     = false
}

variable "forwarded_alarm_name_prefixes" {
  description = "Only forward alarms whose name starts with one of these. Empty forwards every alarm in the account — set this when other projects share it."
  type        = list(string)
  default     = []
}

variable "ecs_cluster_names" {
  description = "ECS clusters the Lambda may inspect for service and task context. Empty grants read access to all clusters in the account."
  type        = list(string)
  default     = []
}

variable "grafana_base_url" {
  description = "Base URL of the Grafana instance, e.g. https://grafana.example.com. Leave empty to disable Grafana enrichment."
  type        = string
  default     = ""
}

variable "splunk_base_url" {
  description = "Base URL of the Splunk search head REST API, e.g. https://splunk.example.com:8089. Leave empty to disable Splunk enrichment."
  type        = string
  default     = ""
}

variable "splunk_index" {
  description = "Splunk index searched for correlating log events."
  type        = string
  default     = "main"
}

variable "context_lookback_minutes" {
  description = "How far back to pull logs, metrics and deployment history when building incident context."
  type        = number
  default     = 30
}

variable "slack_channel" {
  description = "Slack channel the summary is posted to. Informational only when using an incoming webhook, which is already channel-bound."
  type        = string
  default     = "#incidents"
}

# ---------------------------------------------------------------------------
# CI/CD
# ---------------------------------------------------------------------------

variable "github_repository" {
  description = "GitHub repository allowed to assume the deployment role, as `owner/repo`. Empty skips creation of the OIDC role."
  type        = string
  default     = ""
}

variable "github_deploy_branches" {
  description = "Git refs permitted to assume the deployment role."
  type        = list(string)
  default     = ["main"]
}

variable "github_environments" {
  description = "GitHub Actions environments permitted to assume the deployment role. A job that declares `environment:` gets an environment-scoped OIDC subject rather than a branch-scoped one, so every environment used by a workflow must be listed here."
  type        = list(string)
  default     = ["plan", "production"]
}

variable "state_bucket" {
  description = "S3 bucket holding Terraform state. Must match the literal in backend.tf; used to grant the deployment role state access."
  type        = string
  default     = "bokiti123"
}

variable "state_lock_table" {
  description = "DynamoDB table used for state locking. Must match the literal in backend.tf; used to grant the deployment role lock access."
  type        = string
  default     = "family_dyning"
}

variable "create_github_oidc_provider" {
  description = "Create the GitHub OIDC provider. Set false when the account already has one, since it is an account-wide singleton."
  type        = bool
  default     = true
}
