data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  partition  = data.aws_partition.current.partition
  region     = data.aws_region.current.name
  account_id = data.aws_caller_identity.current.account_id

  # Scope ECS reads to named clusters when the caller supplied an allowlist.
  # Empty means "any cluster", which is the pragmatic default for a summarizer
  # that must describe whatever service an alert points at.
  ecs_cluster_arns = length(var.ecs_cluster_names) > 0 ? [
    for name in var.ecs_cluster_names :
    "arn:${local.partition}:ecs:${local.region}:${local.account_id}:cluster/${name}"
  ] : ["*"]
}

# ---------------------------------------------------------------------------
# Execution role
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }

    # Confused-deputy guard: only this account's Lambda service may assume it.
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name                 = "${var.name_prefix}-lambda-exec"
  description          = "Execution role for the AI incident summarizer Lambda."
  assume_role_policy   = data.aws_iam_policy_document.assume_role.json
  max_session_duration = 3600
  permissions_boundary = var.permissions_boundary_arn

  tags = var.tags
}

# ---------------------------------------------------------------------------
# Logging — scoped to this function's log group, not the managed
# AWSLambdaBasicExecutionRole which grants logs:* on every group.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "logging" {
  statement {
    sid     = "CreateOwnLogStream"
    effect  = "Allow"
    actions = ["logs:CreateLogStream", "logs:PutLogEvents"]
    resources = [
      var.log_group_arn,
      "${var.log_group_arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "logging" {
  name   = "logging"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.logging.json
}

# ---------------------------------------------------------------------------
# Bedrock — inference only. No model management, no training data access.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "bedrock" {
  statement {
    sid       = "InvokeSummarizationModel"
    effect    = "Allow"
    actions   = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream"]
    resources = var.bedrock_invoke_arns
  }

  dynamic "statement" {
    for_each = var.bedrock_guardrail_arn == null ? [] : [var.bedrock_guardrail_arn]

    content {
      sid       = "ApplyGuardrail"
      effect    = "Allow"
      actions   = ["bedrock:ApplyGuardrail"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "bedrock" {
  name   = "bedrock-invoke"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.bedrock.json
}

# ---------------------------------------------------------------------------
# Incident context gathering — read-only across ECS, CloudWatch and X-Ray.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "context" {
  # ECS List* and Describe* APIs do not support resource-level permissions, so
  # they are granted on "*" and constrained by condition where possible.
  statement {
    sid    = "DiscoverEcsResources"
    effect = "Allow"
    actions = [
      "ecs:ListClusters",
      "ecs:ListServices",
      "ecs:ListTasks",
      "ecs:ListTaskDefinitions",
      "ecs:DescribeClusters",
      "ecs:DescribeTaskDefinition",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "DescribeEcsWorkloads"
    effect = "Allow"
    actions = [
      "ecs:DescribeServices",
      "ecs:DescribeTasks",
      "ecs:ListServicesByNamespace",
    ]
    resources = ["*"]

    dynamic "condition" {
      for_each = length(var.ecs_cluster_names) > 0 ? [1] : []

      content {
        test     = "ArnEquals"
        variable = "ecs:cluster"
        values   = local.ecs_cluster_arns
      }
    }
  }

  statement {
    sid    = "ReadMetrics"
    effect = "Allow"
    actions = [
      "cloudwatch:GetMetricData",
      "cloudwatch:GetMetricStatistics",
      "cloudwatch:ListMetrics",
      "cloudwatch:DescribeAlarms",
      "cloudwatch:DescribeAlarmHistory",
    ]
    resources = ["*"]
  }

  # Logs Insights queries are started against arbitrary application log groups,
  # which are not known at plan time.
  statement {
    sid    = "QueryApplicationLogs"
    effect = "Allow"
    actions = [
      "logs:StartQuery",
      "logs:GetQueryResults",
      "logs:StopQuery",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "logs:FilterLogEvents",
      "logs:GetLogEvents",
    ]
    resources = ["arn:${local.partition}:logs:${local.region}:${local.account_id}:log-group:*"]
  }

  statement {
    sid    = "ReadTraceSummaries"
    effect = "Allow"
    actions = [
      "xray:GetTraceSummaries",
      "xray:BatchGetTraces",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "context" {
  name   = "incident-context-read"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.context.json
}

# ---------------------------------------------------------------------------
# Secrets — read only the three integration secrets, never the whole namespace.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "secrets" {
  statement {
    sid       = "ReadIntegrationSecrets"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"]
    resources = values(var.secret_arns)
  }

  dynamic "statement" {
    for_each = var.secrets_kms_key_arn == null ? [] : [var.secrets_kms_key_arn]

    content {
      sid       = "DecryptSecrets"
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = [statement.value]

      condition {
        test     = "StringEquals"
        variable = "kms:ViaService"
        values   = ["secretsmanager.${local.region}.amazonaws.com"]
      }
    }
  }
}

resource "aws_iam_role_policy" "secrets" {
  name   = "read-integration-secrets"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.secrets.json
}

# ---------------------------------------------------------------------------
# Deployment artifacts and asynchronous failure handling
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "runtime" {
  statement {
    sid       = "ReadDeploymentPackage"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:GetObjectVersion"]
    resources = ["${var.artifacts_bucket_arn}/*"]
  }

  statement {
    sid       = "SendToDeadLetterQueue"
    effect    = "Allow"
    actions   = ["sqs:SendMessage"]
    resources = [var.dlq_arn]
  }
}

resource "aws_iam_role_policy" "runtime" {
  name   = "runtime"
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.runtime.json
}
