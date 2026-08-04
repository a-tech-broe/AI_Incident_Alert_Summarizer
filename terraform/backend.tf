# Partial backend configuration.
#
# Values are supplied at init time so the same code can target multiple
# environments without editing tracked files:
#
#   terraform init \
#     -backend-config="bucket=my-tfstate-bucket" \
#     -backend-config="key=ai-incident-summarizer/dev/terraform.tfstate" \
#     -backend-config="region=us-east-1" \
#     -backend-config="dynamodb_table=my-tfstate-locks"
#
# CI passes these from repository variables (see .github/workflows/).
terraform {
  backend "s3" {
    encrypt = true
  }
}
