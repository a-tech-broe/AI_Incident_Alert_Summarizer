# AI Incident Summarizer

An AI-assisted incident management flow for AWS. A Grafana alert arrives, a
Lambda gathers context from ECS, CloudWatch, Grafana and Splunk in parallel,
Amazon Bedrock turns that into a triage summary, and the result lands in Slack —
typically before the on-call engineer has finished opening a console tab.

Infrastructure is Terraform modules; delivery is GitHub Actions with OIDC.

The original project brief is preserved at [`docs/SPEC.md`](docs/SPEC.md).

```text
Grafana ──webhook──►  Lambda Function URL  ─┐
                                            ├──►  Summarizer Lambda  ──►  Slack
AWS event sources ──►  EventBridge rule  ───┘            │
                                                         ├─► ECS         (deployments, task failures)
                                                         ├─► CloudWatch  (alarms, metrics, error logs)
                                                         ├─► Grafana     (rule definition, silences)
                                                         ├─► Splunk      (correlated log patterns)
                                                         └─► Bedrock     (summary generation)
```

---

## What it produces

For each firing alert, a Slack message with a fixed layout:

| Field | Purpose |
| --- | --- |
| **Headline** | One line: what is broken and where |
| **Severity** | The model's assessment, shown alongside what the alert claimed |
| **Impact** | Who is affected, in user-facing terms |
| **Probable cause** | The likeliest explanation with its supporting evidence |
| **Confidence** | high / medium / low — an explicit trust signal on the cause |
| **Evidence** | Specific observations, each traceable to a telemetry source |
| **Recommended actions** | Ordered, concrete, lowest-risk first |
| **Gaps** | Telemetry that was unavailable and would have changed the analysis |

The confidence rating and the gaps list exist because a confidently wrong root
cause is worse than no summary — it sends the responder down a dead end while
the outage continues.

> The brief sketched confidence as a percentage ("85% confidence"). This
> implementation uses high/medium/low instead: a language model's self-reported
> percentage is not calibrated, and a precise-looking number invites more trust
> than it has earned. Change `prompt.py` if you want the numeric form.

---

## Repository layout

```text
terraform/
├── modules/
│   ├── s3/            Deployment artifact bucket (versioned, TLS-only, lifecycle)
│   ├── iam/           Least-privilege execution role
│   ├── lambda/        Function, alias, function URL, async failure handling
│   ├── eventbridge/   Custom bus, routing rule, archive, DLQ wiring
│   ├── secrets/       Secrets Manager containers with placeholder versions
│   ├── cloudwatch/    Log group, metric filters, alarms, dashboard
│   ├── bedrock/       Guardrail and the invoke ARNs the role needs
│   └── github-oidc/   Deployment role trusted by GitHub Actions
├── main.tf            Wires the modules together
├── variables.tf       Every input, with validation
├── outputs.tf         Values the pipeline and operators consume
├── terraform.tfvars   Non-secret defaults
└── backend.tf         Partial S3 backend config

lambda/
├── app.py             Handler: auth, routing, orchestration, failure handling
├── alert.py           Payload normalization across ingestion paths
├── config.py          Configuration, structured logging, clients, deadline
├── ecs.py             Deployment state and task failure context
├── cloudwatch.py      Alarms, metrics, aggregated error logs
├── grafana.py         Rule definition, sibling alerts, active silences
├── splunk.py          Correlated log patterns
├── prompt.py          Prompt construction and Bedrock invocation
├── slack.py           Block Kit formatting and delivery
├── http_client.py     Shared HTTP with per-purpose retry budgets
└── tests/             78 tests, no AWS calls required

.github/workflows/
├── ci.yml             fmt, validate, tflint, checkov, ruff, pytest
├── terraform-plan.yml Plan on PR, posted as a comment
└── deploy.yml         plan → approval → apply → package → upload → update
```

---

## Design decisions

**Enrichment never blocks notification.** Every collector returns a status dict
rather than raising, and the whole gather stage runs under a deadline that
reserves 22 seconds for Bedrock. A degraded Splunk costs detail, not the page.

**Delivery is the last thing to fail.** If Bedrock errors or returns unparseable
output, the raw alert still reaches Slack via `post_fallback`. The failure mode
of an incident-response tool must be "less useful", never "silent".

**Unavailable context is stated, not omitted.** The prompt lists sources that
could not be reached, so the model can distinguish "ECS reported healthy" from
"ECS was never queried" — otherwise absence of evidence reads as evidence of
absence, and the summary overstates its own completeness.

**Alert payloads are treated as untrusted.** Telemetry is fenced inside
`<telemetry>` tags and never interpolated into instructions; service labels are
stripped of SPL metacharacters before reaching a Splunk search; an optional
Bedrock guardrail backstops both.

**Terraform owns configuration, the pipeline owns code.** The Lambda module
uploads a bootstrap package and then ignores changes to the code attributes, so
`terraform apply` and `update-function-code` never fight over the same field.
Without this the first apply would fail — there is no artifact yet — and every
later apply would roll the function back to whatever code Terraform last saw.

**The DLQ lives at the root, not in the EventBridge module.** Both the IAM role
and the EventBridge rule need its ARN; nesting it in either would close a
dependency cycle (`iam → eventbridge → lambda → iam`).

---

## Setup

### Prerequisites

- Terraform ≥ 1.6, Python 3.13, AWS CLI
- Remote state is already configured in `terraform/backend.tf` — bucket
  `bokiti123`, lock table `family_dyning`, both in `us-east-1`
- **Bedrock model access requested** for your account and region — the first
  apply succeeds without it, but every invocation will fail with
  `AccessDeniedException` until it is granted

### 1. Configure and apply

```bash
make tf-init
terraform -chdir=terraform plan
terraform -chdir=terraform apply
```

Edit `terraform/terraform.tfvars` first if you want Grafana/Splunk enrichment,
ECS cluster scoping, or the OIDC deployment role — all are off by default so the
stack applies cleanly on an empty account.

### 2. Populate the secrets

Terraform creates the secret containers with a placeholder. The Lambda fails
closed on any secret still holding `REPLACE_ME`, so populate them before the
first real alert:

```bash
PREFIX=ai-incident-summarizer-dev

aws secretsmanager put-secret-value --secret-id "$PREFIX/slack-webhook" \
  --secret-string '{"value":"https://hooks.slack.com/services/..."}'

aws secretsmanager put-secret-value --secret-id "$PREFIX/webhook-token" \
  --secret-string "{\"value\":\"$(openssl rand -hex 32)\"}"

# Only if the corresponding *_base_url variable is set:
aws secretsmanager put-secret-value --secret-id "$PREFIX/grafana-token" \
  --secret-string '{"value":"glsa_..."}'
aws secretsmanager put-secret-value --secret-id "$PREFIX/splunk-token" \
  --secret-string '{"value":"..."}'
```

### 3. Point Grafana at it

Create a webhook contact point using the `lambda_function_url` output, with a
custom header `X-Webhook-Token` set to the value you generated above. The
Function URL is unauthenticated at the AWS layer — Grafana cannot sign SigV4 —
so this shared secret is what protects the endpoint.

For AWS-native producers, publish to the EventBridge bus instead:

```bash
aws events put-events --entries '[{
  "EventBusName": "ai-incident-summarizer-dev-alerts",
  "Source": "grafana.alerts",
  "DetailType": "GrafanaAlert",
  "Detail": "{\"alerts\":[{\"status\":\"firing\",\"labels\":{\"alertname\":\"Test\"}}]}"
}]'
```

### 4. Wire up CI/CD

Set `github_repository` in tfvars, re-apply, then configure the repository:

| Repository variable | Value |
| --- | --- |
| `AWS_ROLE_ARN` | the `github_actions_role_arn` output |
| `AWS_REGION` | your region |

Backend settings are no longer repository variables — they are literals in
`terraform/backend.tf`.

Create two GitHub environments: `plan` (no reviewers) and `production` (required
reviewers). The `production` environment is what makes the apply step pause for
approval.

---

## Development

```bash
make install      # dev dependencies
make check        # everything CI runs: validate, lint, test
make test         # pytest only
make package      # build the arm64 deployment package locally
```

---

## The pipeline

```text
push to main
    │
    ├─► fmt → validate → plan          (environment: plan)
    │       └─ -detailed-exitcode: skips apply entirely when nothing changed
    │
    ├─► APPROVAL GATE                  (environment: production)
    │       └─ applies the reviewed plan file, not a fresh re-plan
    │
    └─► package → upload → update → smoke test → promote alias
            └─ arm64 wheels, deterministic zip, alias only moves after the
               smoke test passes
```

Rollback is deliberately manual — a failed smoke test may mean a bad deploy or a
broken dependency, and auto-reverting on the second case hides the real fault:

```bash
aws lambda list-versions-by-function --function-name <name>
aws lambda update-alias --function-name <name> --name live --function-version <previous>
```

---

## Operational surface

Created by the `cloudwatch` module:

- **Dashboard** `ai-incident-summarizer-<env>-overview` — invocations, duration
  against the timeout, pipeline failures, recent error logs
- **Alarms** — Lambda errors, throttles, p99 duration past 80% of timeout,
  non-empty DLQ, Bedrock failures, Slack delivery failures
- **Custom metrics** from the handler's own structured logs, under
  `ai-incident-summarizer-<env>/Summarizer`

Every log line is single-line JSON with `level` and `event` fields. The metric
filters key on those, so replacing the logger would silently break the alarms.

Useful queries:

```text
fields @timestamp, event, message | filter level = "ERROR" | sort @timestamp desc
fields @timestamp, sources_available, duration_ms | filter event = "context_gathered"
fields @timestamp, input_tokens, output_tokens | filter event = "bedrock_invoke_succeeded"
```

---

## Cost

Dominated by Bedrock. At roughly 4k input / 600 output tokens per alert, expect
a few cents per summary on Claude Sonnet; Lambda, EventBridge and CloudWatch are
rounding errors at incident volumes. Two controls bound the worst case:
`lambda_reserved_concurrency` caps parallel invocations during an alert storm,
and resolved alerts are dropped before any model call.

---

## Development phases

The brief's phasing, and where each landed:

| Phase | Scope | Status |
| --- | --- | --- |
| 1 | Terraform foundation — S3, IAM, Lambda, EventBridge, Secrets | Done |
| 2 | Lambda receives and normalizes alerts | Done |
| 3 | ECS, CloudWatch and Grafana enrichment | Done |
| 4 | Splunk integration | Done |
| 5 | Bedrock prompt engineering | Done |
| 6 | Slack notifications | Done |
| 7 | GitHub Actions deployment | Done |

The brief listed a VPC module as optional. It is not included: the Lambda talks
only to public AWS and SaaS endpoints, so VPC attachment would add NAT gateway
cost and cold-start latency for no isolation benefit. Add it if Splunk or
Grafana sit inside private networking.

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for operating and troubleshooting the
deployed system.
