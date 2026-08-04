variable "name_prefix" {
  description = "Prefix for the role name. Also bounds which IAM roles the pipeline may manage."
  type        = string
}

variable "github_repository" {
  description = "Repository allowed to assume the role, as `owner/repo`."
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", var.github_repository))
    error_message = "github_repository must be in owner/repo form."
  }
}

variable "allowed_branches" {
  description = "Branches whose workflow runs may assume the role."
  type        = list(string)
  default     = ["main"]
}

variable "allowed_environments" {
  description = "GitHub Actions environments whose jobs may assume the role. A job declaring `environment:` presents an environment-scoped subject, so list every environment any workflow uses — not just the approval-gated one."
  type        = list(string)
  default     = ["plan", "production"]
}

variable "allow_pull_requests" {
  description = "Allow pull_request-triggered runs to assume the role. Needed for plan-on-PR; grant read-heavy permissions only."
  type        = bool
  default     = true
}

variable "create_oidc_provider" {
  description = "Create the GitHub OIDC provider. It is an account-wide singleton, so set false when one already exists."
  type        = bool
  default     = true
}

variable "thumbprints" {
  description = "Root CA thumbprints for the GitHub OIDC endpoint. AWS validates against its own trust store, so these are effectively vestigial but the API still requires them."
  type        = list(string)
  default = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcd",
  ]
}

variable "artifacts_bucket_arn" {
  description = "ARN of the deployment artifacts bucket."
  type        = string
}

variable "lambda_function_arn" {
  description = "ARN of the Lambda the pipeline may update."
  type        = string
}

variable "state_bucket_arn" {
  description = "ARN of the Terraform state bucket. Null skips the state policy, e.g. when state access is granted elsewhere."
  type        = string
  default     = null
}

variable "state_lock_table_arn" {
  description = "ARN of the DynamoDB state lock table."
  type        = string
  default     = null
}

variable "attach_terraform_policy" {
  description = "Attach the built-in policy allowing the pipeline to manage this stack's resources."
  type        = bool
  default     = true
}

variable "permissions_boundary_arn" {
  description = "Optional permissions boundary for the deployment role."
  type        = string
  default     = null
}

variable "tags" {
  description = "Tags applied to created resources."
  type        = map(string)
  default     = {}
}
