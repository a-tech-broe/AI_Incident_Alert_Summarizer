# Bootstrap

Getting from an empty account to a working pipeline. Only needed once per
environment; after this the GitHub Actions pipeline is self-sufficient.

There is a deliberate ordering problem here: the pipeline authenticates with an
IAM role that Terraform creates, so the first apply has to happen from a
workstation. That is what this document covers.

---

## Current state of account `694992586025`

Established by inspection, not assumption:

| Thing | State |
| --- | --- |
| Terraform state in `s3://bokiti123` | Empty — never applied |
| Lock table `family_dyning` | ACTIVE, `LockID` hash key |
| GitHub OIDC provider | **Already exists** (`create_github_oidc_provider = false` is set for this reason) |
| Repository variables | None set |
| Deploy role | Does not exist yet |

The IAM user `Parah` has one policy, `ParahAccess`, scoped to an unrelated
`banking-platform` project. It already covers what this stack needs for
Secrets Manager, KMS, CloudWatch, Logs, SNS, and the Terraform state backend.
It does **not** cover Lambda, EventBridge, SQS, the artifact bucket, or IAM
roles named `ai-incident-summarizer-*`.

`ParahAccess` cannot simply be extended: it is 5627 of the 6144-character hard
limit for a managed policy, and the missing permissions need roughly 1290. Hence
a separate policy.

---

## Step 1 — Create and attach the access policy

Requires an identity that can create IAM policies. `Parah` cannot: its
`iam:CreatePolicy` grant is scoped to the `ParahAccess` ARN itself, so creating
a differently-named policy is denied. Use the account root or an admin user.

The document is [`bootstrap-iam-policy.json`](bootstrap-iam-policy.json) —
1051 characters, five statements, every one scoped to `ai-incident-summarizer-*`.

```bash
aws iam create-policy \
  --policy-name AiIncidentSummarizerAccess \
  --description "Deploy permissions for the AI incident summarizer stack." \
  --policy-document file://docs/bootstrap-iam-policy.json

aws iam attach-user-policy \
  --user-name Parah \
  --policy-arn arn:aws:iam::694992586025:policy/AiIncidentSummarizerAccess
```

Or in the console: **IAM → Policies → Create policy → JSON**, paste the file,
name it `AiIncidentSummarizerAccess`, then attach it to user `Parah`.

Verify it took effect:

```bash
aws iam list-attached-user-policies --user-name Parah \
  --query 'AttachedPolicies[].PolicyName'
```

## Step 2 — Request Bedrock model access

Independent of everything else, and easy to forget: the stack applies cleanly
without it and then every invocation fails with `AccessDeniedException`.

Enable **Anthropic → Claude Sonnet 4.5** at
<https://us-east-1.console.aws.amazon.com/bedrock/home?region=us-east-1#/modelaccess>.
`bedrock_model_id` is a cross-region inference profile (`us.` prefix), which
routes across `us-east-1`, `us-east-2` and `us-west-2`. Model access is granted
per region, so grant it in **all three** — otherwise invocations succeed until
the profile routes elsewhere, then fail intermittently.

To avoid that entirely, drop the prefix in `terraform.tfvars`:
`bedrock_model_id = "anthropic.claude-sonnet-4-5-20250929-v1:0"`. Single region,
one grant, lower burst headroom.

## Step 3 — First apply

```bash
make tf-init
terraform -chdir=terraform plan     # review before applying
terraform -chdir=terraform apply
```

## Step 4 — Populate the secrets

Terraform creates the containers with a `REPLACE_ME` placeholder, and the Lambda
fails closed on it.

```bash
PREFIX=ai-incident-summarizer-dev

aws secretsmanager put-secret-value --secret-id "$PREFIX/slack-webhook" \
  --secret-string '{"value":"https://hooks.slack.com/services/..."}'

aws secretsmanager put-secret-value --secret-id "$PREFIX/webhook-token" \
  --secret-string "{\"value\":\"$(openssl rand -hex 32)\"}"
```

`grafana-token` and `splunk-token` only matter if the corresponding
`*_base_url` variable is set; leave them as placeholders otherwise and those
enrichment sources report themselves unavailable.

## Step 5 — Publish the repository variables

This closes the loop — the role Terraform just created becomes the identity the
pipeline assumes.

```bash
gh variable set AWS_REGION --body us-east-1
gh variable set AWS_ROLE_ARN \
  --body "$(terraform -chdir=terraform output -raw github_actions_role_arn)"

gh variable list
```

The workflows fail fast with a named error if either is missing.

## Step 6 — Create the GitHub environments

- `plan` — no reviewers
- `production` — required reviewers

The `production` environment is what makes the apply step pause for approval;
without it the gate silently does nothing.

Both names must appear in `github_environments` in `terraform.tfvars`, because a
job declaring `environment:` presents an environment-scoped OIDC subject rather
than a branch-scoped one.

---

## Optional: publishing Checkov results

`ENABLE_CODE_SCANNING=true` turns on SARIF upload to the Security tab. It only
works if the repository is public or has Advanced Security — this repository is
private without it, so the step stays skipped and findings go to the job summary
and a workflow artifact instead.

## If you later enable Bedrock guardrails

`enable_bedrock_guardrail = true` adds guardrail resources, which need Bedrock
management permissions the policy above deliberately omits:

```json
{
  "Sid": "ManageSummarizerGuardrail",
  "Effect": "Allow",
  "Action": [
    "bedrock:CreateGuardrail", "bedrock:UpdateGuardrail",
    "bedrock:DeleteGuardrail", "bedrock:GetGuardrail",
    "bedrock:CreateGuardrailVersion", "bedrock:ListGuardrails",
    "bedrock:TagResource", "bedrock:UntagResource", "bedrock:ListTagsForResource"
  ],
  "Resource": "*"
}
```

Guardrail ARNs are generated, so they cannot be scoped by name at create time.
