# Remote state in an existing bucket and lock table.
#
# Backend blocks cannot reference variables, so these are literals. The same
# names are mirrored into var.state_bucket / var.state_lock_table so the OIDC
# deployment role can be granted access to them — keep the two in sync.
#
# To target a different environment, override just the key at init time; a
# fully-specified backend still accepts partial overrides:
#
#   terraform init -reconfigure \
#     -backend-config="key=ai-incident-summarizer/prod/terraform.tfstate"
terraform {
  backend "s3" {
    bucket         = "bokiti123"
    key            = "ai-incident-summarizer/dev/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "family_dyning"
    encrypt        = true
  }
}
