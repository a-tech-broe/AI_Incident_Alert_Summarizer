locals {
  name_prefix   = "${var.project_name}-${var.environment}"
  function_name = "${var.project_name}-${var.environment}-summarizer"

  # Derived from the same names hardcoded in backend.tf. Terraform cannot read
  # its own backend configuration, so the values are mirrored through variables.
  state_bucket_arn = "arn:${data.aws_partition.current.partition}:s3:::${var.state_bucket}"

  state_lock_table_arn = format(
    "arn:%s:dynamodb:%s:%s:table/%s",
    data.aws_partition.current.partition,
    var.aws_region,
    data.aws_caller_identity.current.account_id,
    var.state_lock_table,
  )

  common_tags = merge(
    {
      Project     = var.project_name
      Environment = var.environment
      ManagedBy   = "terraform"
      Component   = "ai-incident-management"
    },
    var.tags
  )

  # Secrets the Lambda reads at cold start. Terraform creates the containers and
  # a placeholder version; real values are written out of band so they never
  # land in state or in a plan file.
  secrets = {
    slack_webhook = {
      description = "Slack incoming webhook URL used to post incident summaries."
    }
    grafana_token = {
      description = "Grafana service account token for dashboard and alert-rule lookups."
    }
    splunk_token = {
      description = "Splunk REST API bearer token used for correlating log searches."
    }
    webhook_token = {
      description = "Shared secret Grafana sends in the X-Webhook-Token header. The handler rejects Function URL requests without it."
    }
  }
}

# ---------------------------------------------------------------------------
# Dead-letter queue
#
# Declared at the root rather than inside the EventBridge module: both the IAM
# role and the EventBridge rule need its ARN, and nesting it in either module
# would close a dependency cycle (iam -> eventbridge -> lambda -> iam).
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "dlq" {
  name                      = "${local.name_prefix}-dlq"
  message_retention_seconds = 1209600 # 14 days, the maximum
  sqs_managed_sse_enabled   = true

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# S3 — Lambda deployment artifacts
# ---------------------------------------------------------------------------

module "artifacts" {
  source = "./modules/s3"

  bucket_name        = "${local.name_prefix}-artifacts-${data.aws_caller_identity.current.account_id}"
  versioning_enabled = true

  # Deployment packages are cheap to rebuild; keep a short tail for rollback.
  noncurrent_version_expiration_days = 90
  abort_incomplete_upload_days       = 7

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Secrets Manager — third-party credentials
# ---------------------------------------------------------------------------

module "secrets" {
  source = "./modules/secrets"

  name_prefix             = local.name_prefix
  secrets                 = local.secrets
  recovery_window_in_days = var.environment == "prod" ? 30 : 0

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# CloudWatch — log group and operational alarms
# ---------------------------------------------------------------------------

module "observability" {
  source = "./modules/cloudwatch"

  name_prefix        = local.name_prefix
  function_name      = local.function_name
  log_retention_days = var.log_retention_days
  alarm_emails       = var.alarm_email_subscriptions
  dlq_queue_name     = aws_sqs_queue.dlq.name
  lambda_timeout     = var.lambda_timeout

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Bedrock — model configuration and optional guardrail
# ---------------------------------------------------------------------------

module "bedrock" {
  source = "./modules/bedrock"

  name_prefix      = local.name_prefix
  model_id         = var.bedrock_model_id
  enable_guardrail = var.enable_bedrock_guardrail

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# IAM — least-privilege execution role
# ---------------------------------------------------------------------------

module "iam" {
  source = "./modules/iam"

  name_prefix   = local.name_prefix
  function_name = local.function_name

  log_group_arn         = module.observability.log_group_arn
  artifacts_bucket_arn  = module.artifacts.bucket_arn
  secret_arns           = module.secrets.secret_arns
  bedrock_invoke_arns   = module.bedrock.invoke_resource_arns
  bedrock_guardrail_arn = module.bedrock.guardrail_arn
  ecs_cluster_names     = var.ecs_cluster_names
  dlq_arn               = aws_sqs_queue.dlq.arn

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# Lambda — the summarizer itself
# ---------------------------------------------------------------------------

module "lambda" {
  source = "./modules/lambda"

  function_name = local.function_name
  description   = "Correlates a Grafana alert with ECS, CloudWatch and Splunk context, summarizes it with Bedrock, and posts to Slack."

  role_arn    = module.iam.role_arn
  runtime     = var.lambda_runtime
  memory_size = var.lambda_memory_size
  timeout     = var.lambda_timeout
  handler     = "app.lambda_handler"

  artifacts_bucket = module.artifacts.bucket_id

  reserved_concurrent_executions = var.lambda_reserved_concurrency
  log_group_name                 = module.observability.log_group_name
  dead_letter_target_arn         = aws_sqs_queue.dlq.arn

  enable_function_url = var.enable_function_url

  environment_variables = {
    LOG_LEVEL                = var.lambda_log_level
    ENVIRONMENT              = var.environment
    PROJECT_NAME             = var.project_name
    BEDROCK_MODEL_ID         = var.bedrock_model_id
    BEDROCK_MAX_TOKENS       = tostring(var.bedrock_max_tokens)
    BEDROCK_TEMPERATURE      = tostring(var.bedrock_temperature)
    BEDROCK_GUARDRAIL_ID     = module.bedrock.guardrail_id
    BEDROCK_GUARDRAIL_VER    = module.bedrock.guardrail_version
    WEBHOOK_TOKEN_SECRET_ID  = module.secrets.secret_names["webhook_token"]
    SLACK_WEBHOOK_SECRET_ID  = module.secrets.secret_names["slack_webhook"]
    SLACK_CHANNEL            = var.slack_channel
    GRAFANA_BASE_URL         = var.grafana_base_url
    GRAFANA_TOKEN_SECRET_ID  = module.secrets.secret_names["grafana_token"]
    SPLUNK_BASE_URL          = var.splunk_base_url
    SPLUNK_TOKEN_SECRET_ID   = module.secrets.secret_names["splunk_token"]
    SPLUNK_INDEX             = var.splunk_index
    CONTEXT_LOOKBACK_MINUTES = tostring(var.context_lookback_minutes)
    ECS_CLUSTER_ALLOWLIST    = join(",", var.ecs_cluster_names)
  }

  tags = local.common_tags

  # The log group must exist before the function so Lambda does not create an
  # unmanaged one with never-expiring retention on first invocation.
  depends_on = [module.observability]
}

# ---------------------------------------------------------------------------
# EventBridge — alert ingestion, routing and dead-lettering
# ---------------------------------------------------------------------------

module "eventbridge" {
  source = "./modules/eventbridge"

  name_prefix        = local.name_prefix
  target_lambda_arn  = module.lambda.function_arn
  target_lambda_name = module.lambda.function_name

  event_sources                 = var.event_sources
  event_detail_types            = var.event_detail_types
  max_retry_attempts            = var.event_max_retry_attempts
  forward_cloudwatch_alarms     = var.forward_cloudwatch_alarms
  forwarded_alarm_name_prefixes = var.forwarded_alarm_name_prefixes
  dlq_arn                       = aws_sqs_queue.dlq.arn
  dlq_url                       = aws_sqs_queue.dlq.url

  tags = local.common_tags
}

# ---------------------------------------------------------------------------
# GitHub Actions OIDC deployment role
# ---------------------------------------------------------------------------

module "github_oidc" {
  source = "./modules/github-oidc"
  count  = var.github_repository == "" ? 0 : 1

  name_prefix          = local.name_prefix
  github_repository    = var.github_repository
  allowed_branches     = var.github_deploy_branches
  allowed_environments = var.github_environments
  create_oidc_provider = var.create_github_oidc_provider

  artifacts_bucket_arn = module.artifacts.bucket_arn
  lambda_function_arn  = module.lambda.function_arn

  # Without these the pipeline can assume the role but not read or lock state,
  # so every CI run fails at `terraform init`.
  state_bucket_arn     = local.state_bucket_arn
  state_lock_table_arn = local.state_lock_table_arn

  tags = local.common_tags
}
