data "aws_caller_identity" "current" {}
data "aws_region" "current" {}
data "aws_partition" "current" {}

locals {
  partition  = data.aws_partition.current.partition
  account_id = data.aws_caller_identity.current.account_id

  provider_url = "https://token.actions.githubusercontent.com"

  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github[0].arn : "arn:${local.partition}:iam::${local.account_id}:oidc-provider/token.actions.githubusercontent.com"

  repo_owner = split("/", var.github_repository)[0]
  repo_name  = split("/", var.github_repository)[1]

  # GitHub issues `sub` in one of two shapes, and which one a repository gets is
  # a GitHub-side setting we do not control:
  #
  #   repo:owner/name:...                    the historical form
  #   repo:owner@<owner-id>/name@<repo-id>:… the immutable-subject form
  #
  # The immutable form embeds numeric IDs so a renamed or re-created repository
  # cannot inherit a trust relationship granted to its old name. The IDs are not
  # knowable at plan time, so the second form is matched with wildcards. Check a
  # repository's actual shape with:
  #
  #   gh api repos/OWNER/REPO/actions/oidc/customization/sub
  #
  # Trusting only one form is the failure this guards against: every other part
  # of the configuration looks correct and STS still returns
  # "Not authorized to perform sts:AssumeRoleWithWebIdentity".
  repo_forms = [
    var.github_repository,
    "${local.repo_owner}@*/${local.repo_name}@*",
  ]

  # Branch refs cover plan on push; the environment claims cover the
  # approval-gated apply job, which runs against a protected GitHub environment
  # rather than a bare branch.
  branch_subjects = flatten([
    for r in local.repo_forms : [for b in var.allowed_branches : "repo:${r}:ref:refs/heads/${b}"]
  ])

  environment_subjects = flatten([
    for r in local.repo_forms : [for e in var.allowed_environments : "repo:${r}:environment:${e}"]
  ])

  pull_request_subject = var.allow_pull_requests ? [
    for r in local.repo_forms : "repo:${r}:pull_request"
  ] : []

  allowed_subjects = concat(local.branch_subjects, local.environment_subjects, local.pull_request_subject)
}

# Account-wide singleton. Disable creation when another stack already owns it.
resource "aws_iam_openid_connect_provider" "github" {
  count = var.create_oidc_provider ? 1 : 0

  url             = local.provider_url
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = var.thumbprints

  tags = var.tags
}

data "aws_iam_policy_document" "assume_role" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # StringLike on an explicit subject list, not "repo:owner/*", so a fork or a
    # new branch cannot mint credentials.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values   = local.allowed_subjects
    }
  }
}

resource "aws_iam_role" "github_actions" {
  name                 = "${var.name_prefix}-github-actions"
  description          = "Assumed by GitHub Actions via OIDC to plan, apply and deploy."
  assume_role_policy   = data.aws_iam_policy_document.assume_role.json
  max_session_duration = 3600
  permissions_boundary = var.permissions_boundary_arn

  tags = var.tags
}

# ---------------------------------------------------------------------------
# Deployment permissions
#
# Narrow by design: the pipeline uploads a package and points the function at
# it. Infrastructure changes go through the broader terraform policy below.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "deploy" {
  statement {
    sid    = "PublishDeploymentPackage"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:GetObject",
      "s3:GetObjectVersion",
      "s3:ListBucket",
    ]
    resources = [
      var.artifacts_bucket_arn,
      "${var.artifacts_bucket_arn}/*",
    ]
  }

  statement {
    sid    = "UpdateFunctionCode"
    effect = "Allow"
    actions = [
      "lambda:UpdateFunctionCode",
      "lambda:PublishVersion",
      "lambda:UpdateAlias",
      "lambda:GetAlias",
      "lambda:GetFunction",
      "lambda:GetFunctionConfiguration",
      "lambda:ListVersionsByFunction",
      "lambda:InvokeFunction",
    ]
    resources = [
      var.lambda_function_arn,
      "${var.lambda_function_arn}:*",
    ]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "lambda-deploy"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.deploy.json
}

# ---------------------------------------------------------------------------
# Terraform state access
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "state" {
  count = var.state_bucket_arn == null ? 0 : 1

  statement {
    sid       = "ReadWriteState"
    effect    = "Allow"
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${var.state_bucket_arn}/*"]
  }

  statement {
    sid       = "ListStateBucket"
    effect    = "Allow"
    actions   = ["s3:ListBucket"]
    resources = [var.state_bucket_arn]
  }

  dynamic "statement" {
    for_each = var.state_lock_table_arn == null ? [] : [var.state_lock_table_arn]

    content {
      sid       = "StateLocking"
      effect    = "Allow"
      actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem"]
      resources = [statement.value]
    }
  }
}

resource "aws_iam_role_policy" "state" {
  count = var.state_bucket_arn == null ? 0 : 1

  name   = "terraform-state"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.state[0].json
}

# ---------------------------------------------------------------------------
# Infrastructure management
#
# terraform plan needs broad read access; apply needs write access to the
# services this stack owns. Attached as a managed policy so it can be swapped
# for a tighter, org-specific one without touching the module.
# ---------------------------------------------------------------------------

data "aws_iam_policy_document" "terraform" {
  count = var.attach_terraform_policy ? 1 : 0

  statement {
    sid    = "ManageStackResources"
    effect = "Allow"
    actions = [
      "s3:*",
      "lambda:*",
      "events:*",
      "sqs:*",
      "sns:*",
      "logs:*",
      "cloudwatch:*",
      "secretsmanager:*",
      "bedrock:GetGuardrail",
      "bedrock:CreateGuardrail",
      "bedrock:UpdateGuardrail",
      "bedrock:DeleteGuardrail",
      "bedrock:CreateGuardrailVersion",
      "bedrock:ListGuardrails",
      "bedrock:TagResource",
      "bedrock:UntagResource",
      "bedrock:ListTagsForResource",
    ]
    resources = ["*"]
  }

  # IAM is scoped by name prefix: the pipeline can manage this stack's roles and
  # nothing else, which is what stops a compromised workflow from escalating.
  statement {
    sid    = "ManageStackIamRoles"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:GetRole",
      "iam:UpdateRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:PassRole",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:GetRolePolicy",
      "iam:ListRolePolicies",
      "iam:ListAttachedRolePolicies",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:ListRoleTags",
    ]
    resources = ["arn:${local.partition}:iam::${local.account_id}:role/${var.name_prefix}-*"]
  }

  statement {
    sid    = "ReadOnlyForPlan"
    effect = "Allow"
    actions = [
      "iam:GetOpenIDConnectProvider",
      "iam:ListOpenIDConnectProviders",
      "sts:GetCallerIdentity",
      "ec2:DescribeRegions",
      "ecs:Describe*",
      "ecs:List*",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role_policy" "terraform" {
  count = var.attach_terraform_policy ? 1 : 0

  name   = "terraform-manage-stack"
  role   = aws_iam_role.github_actions.id
  policy = data.aws_iam_policy_document.terraform[0].json
}
