# Default (dev) configuration.
#
# Nothing here is a secret. Credentials live in Secrets Manager and are written
# out of band — see docs/RUNBOOK.md.

project_name = "ai-incident-summarizer"
environment  = "dev"
aws_region   = "us-east-1"

# --- Lambda ---------------------------------------------------------------
lambda_runtime              = "python3.13"
lambda_memory_size          = 512
lambda_timeout              = 60
lambda_reserved_concurrency = 10
lambda_log_level            = "INFO"

# Grafana's webhook contact point posts here. Disable if alerts arrive only via
# EventBridge from AWS-native sources.
enable_function_url = true

# --- Observability --------------------------------------------------------
log_retention_days        = 30
alarm_email_subscriptions = []

# --- Bedrock --------------------------------------------------------------
# Cross-region inference profile. Request model access in the Bedrock console
# for the account and region before the first apply.
bedrock_model_id    = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
bedrock_max_tokens  = 2000
bedrock_temperature = 0.2

# Alert payloads are attacker-influenced. Turn this on for prod.
enable_bedrock_guardrail = false

# --- Alert ingestion ------------------------------------------------------
event_sources            = ["grafana.alerts"]
event_detail_types       = []
event_max_retry_attempts = 2

# --- Downstream integrations ---------------------------------------------
# Empty values disable the corresponding enrichment step; the summarizer
# degrades to whatever context it can gather.
ecs_cluster_names        = []
grafana_base_url         = ""
splunk_base_url          = ""
splunk_index             = "main"
context_lookback_minutes = 30
slack_channel            = "#incidents"

# --- Remote state ---------------------------------------------------------
# These must match the literals in backend.tf. They exist only so the OIDC
# deployment role can be granted access to the state bucket and lock table.
state_bucket     = "bokiti123"
state_lock_table = "family_dyning"

# --- CI/CD ----------------------------------------------------------------
# Set to your repository to create the OIDC deployment role, then publish the
# resulting github_actions_role_arn output as the AWS_ROLE_ARN repo variable.
github_repository      = ""
github_deploy_branches = ["main"]

# Both workflow environments must be listed: a job declaring `environment:` gets
# an environment-scoped OIDC subject, not a branch-scoped one. Omitting "plan"
# makes the plan job fail at AssumeRoleWithWebIdentity.
github_environments = ["plan", "production"]

# The OIDC provider is account-wide. Set false if one already exists.
create_github_oidc_provider = true
