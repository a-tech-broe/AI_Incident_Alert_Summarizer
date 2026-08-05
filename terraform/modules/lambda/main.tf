# Bootstrap package.
#
# The pipeline order in the README is apply -> package -> upload -> update, so
# the function must be creatable before any real artifact exists. Terraform
# uploads a stub, then stops tracking the code attributes (see ignore_changes
# below) and leaves subsequent code updates to the deploy workflow. Without this
# the first apply fails, and every later apply would roll the function back to
# whatever code Terraform last saw.

data "archive_file" "bootstrap" {
  type        = "zip"
  output_path = "${path.module}/.terraform-bootstrap.zip"

  source {
    filename = "app.py"
    content  = <<-PY
      def lambda_handler(event, context):
          raise RuntimeError(
              "Bootstrap placeholder. Deploy real code via the "
              "lambda-deploy GitHub Actions workflow."
          )
    PY
  }
}

resource "aws_s3_object" "bootstrap" {
  bucket = var.artifacts_bucket
  key    = local.s3_key
  source = data.archive_file.bootstrap.output_path
  etag   = data.archive_file.bootstrap.output_md5

  # The deploy workflow overwrites this key. Terraform must not drag it back.
  lifecycle {
    ignore_changes = [etag, source, source_hash]
  }
}

locals {
  s3_key = "${var.function_name}/deployment-package.zip"
}

# ---------------------------------------------------------------------------
# Function
# ---------------------------------------------------------------------------

resource "aws_lambda_function" "this" {
  function_name = var.function_name
  description   = var.description
  role          = var.role_arn
  handler       = var.handler
  runtime       = var.runtime
  memory_size   = var.memory_size
  timeout       = var.timeout
  architectures = [var.architecture]
  publish       = true

  s3_bucket = var.artifacts_bucket
  s3_key    = aws_s3_object.bootstrap.key

  reserved_concurrent_executions = var.reserved_concurrent_executions

  environment {
    variables = var.environment_variables
  }

  # Catches invocations that fail after all async retries, including ones that
  # never reach the handler (init errors, OOM).
  dead_letter_config {
    target_arn = var.dead_letter_target_arn
  }

  tracing_config {
    mode = var.tracing_mode
  }

  # log_format is Text, not JSON, even though the handler emits JSON.
  #
  # Lambda's JSON log format wraps every stdout line in its own envelope, moving
  # the handler's fields to $.message.level and $.message.event. The CloudWatch
  # metric filters match on $.level and $.event, so switching this to JSON
  # silently stops every custom alarm from ever firing.
  logging_config {
    log_format = var.log_format
    log_group  = var.log_group_name
  }

  tags = var.tags

  lifecycle {
    # Code is owned by the deploy workflow from the first deployment onward.
    ignore_changes = [s3_key, s3_object_version, source_code_hash]
  }
}

# A stable alias to point event sources and rollbacks at, so a bad deploy is one
# alias update away from being undone.
resource "aws_lambda_alias" "live" {
  name             = "live"
  description      = "Currently serving version."
  function_name    = aws_lambda_function.this.function_name
  function_version = aws_lambda_function.this.version

  lifecycle {
    ignore_changes = [function_version]
  }
}

# Bounds how long a failed async invocation is retried before it is dead-lettered.
resource "aws_lambda_function_event_invoke_config" "this" {
  function_name                = aws_lambda_function.this.function_name
  maximum_retry_attempts       = var.async_retry_attempts
  maximum_event_age_in_seconds = var.async_max_event_age

  destination_config {
    on_failure {
      destination = var.dead_letter_target_arn
    }
  }
}

# ---------------------------------------------------------------------------
# Function URL
#
# Grafana's webhook contact point can only POST to an HTTPS endpoint, so this is
# the ingestion path when alerts come straight from Grafana rather than through
# an AWS event source. Auth is NONE at the AWS layer because Grafana cannot sign
# SigV4; the handler verifies a shared secret header instead. Keep it disabled
# unless that path is actually in use.
# ---------------------------------------------------------------------------

resource "aws_lambda_function_url" "this" {
  count = var.enable_function_url ? 1 : 0

  function_name      = aws_lambda_function.this.function_name
  authorization_type = var.function_url_auth_type

  cors {
    allow_origins = ["*"]
    allow_methods = ["POST"]
    allow_headers = ["content-type", "x-webhook-token"]
    max_age       = 3600
  }
}
