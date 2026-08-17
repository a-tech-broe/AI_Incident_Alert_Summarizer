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

### Ingestion paths

Transport and payload shape are detected independently, so any producer can
reach it by whichever route it already has:

| Arrives via | Recognized shape |
| --- | --- |
| Function URL (`X-Webhook-Token`) | Grafana unified alerting group — `alerts[]` |
| EventBridge `PutEvents` on the alerts bus | CloudWatch alarm state change — object `state` |
| Direct invocation (console test, manual replay) | Flat `{alertname, severity, service, ...}` |

CloudWatch alarms need no application changes: set `forward_cloudwatch_alarms`
and the stack republishes matching alarms from the default bus onto its own.
`OK` and `INSUFFICIENT_DATA` are dropped before any model call. Wiring an
existing workload in either way: [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md).

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
├── providers.tf       Provider and default tag configuration
├── versions.tf        Terraform and provider version constraints
└── backend.tf         S3 backend config (bucket and lock table are literals)

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
├── requirements.txt   Runtime dependencies (what gets packaged)
├── requirements-dev.txt  Test and lint tooling
└── tests/             100 tests, no AWS calls required

.github/workflows/
├── ci.yml             fmt, validate, tflint, checkov, ruff, pytest
├── terraform-plan.yml Plan on PR, posted as a comment
└── deploy.yml         plan → approval → apply → package → upload → update

scripts/
└── smoke-test.sh      End-to-end probe: EventBridge → Lambda → Slack
                       --resolved (no model call), --cloudwatch (alarm shape),
                       --no-cold-start

docs/
├── BOOTSTRAP.md       First deployment into a fresh account
├── INTEGRATIONS.md    Feeding it from an existing workload
├── RUNBOOK.md         Operating and troubleshooting
├── SPEC.md            The original brief
└── bootstrap-iam-policy.json  Permissions the first apply needs
```

---

## Design decisions

**Enrichment never blocks notification.** Every collector returns a status dict
rather than raising, and the whole gather stage runs under a deadline that
reserves 22 seconds for Bedrock. A degraded Splunk costs detail, not the page.

**Delivery is the last thing to fail.** If Bedrock errors or returns unparseable
output, the raw alert still reaches Slack via `post_fallback`. The failure mode
of an incident-response tool must be "less useful", never "silent".

**A group's alerts are summarized concurrently, under a per-alert budget.** One
Grafana group carries an entry per firing instance, each needing its own
enrichment and its own Bedrock call. Serially, the second alert starts with the
first one's time already spent and the third is killed mid-generation — and a
killed invocation is not a return value, so it escapes the "200 with a failure
count" contract and EventBridge retries the whole batch, re-posting summaries
that already landed. Concurrency turns the sum of those costs into the maximum;
alerts that still do not fit are checked *before* enrichment and delivered raw,
since a generation that cannot finish spends the model call and loses the alert
anyway. The bound is 3 — every worker holds an in-flight Bedrock call, and
model throughput is scarcer than Lambda concurrency.

**Collectors are memoized per invocation.** Alerts in a group share
`commonLabels`, so they routinely carry the same service and cluster. Keyed on
the arguments, so alerts on different services still get their own lookups —
what this removes is the repeated Logs Insights query, the slowest step in the
pipeline and the only one billed per scan.

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
- **Bedrock usable in your account and region** — both the model *authorization*
  and a non-zero *throughput quota*. They are separate grants: an authorized
  model with zero quota fails every invocation with a `ThrottlingException` that
  reads like a transient limit. The first apply succeeds either way; see
  [`docs/RUNBOOK.md`](docs/RUNBOOK.md#bedrock-errors) for how to tell them apart

### 1. Configure and apply

```bash
make tf-init
terraform -chdir=terraform plan
terraform -chdir=terraform apply
```

Edit `terraform/terraform.tfvars` first if you want Grafana/Splunk enrichment,
ECS cluster scoping, CloudWatch alarm forwarding, or the OIDC deployment role —
all are off by default so the stack applies cleanly on an empty account.

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
make lint         # ruff check + format check
make coverage     # tests with a term-missing coverage report
make package      # build the arm64 deployment package locally
make help         # every target
```

The tests stub every AWS and SaaS call, so the suite runs without credentials.
To exercise a deployed stack instead:

```bash
scripts/smoke-test.sh --resolved    # parse and route only, no Bedrock spend
scripts/smoke-test.sh               # full path, ends in a real Slack message
scripts/smoke-test.sh --cloudwatch  # alarm-shaped payload rather than Grafana
```

It publishes one synthetic alert to the bus and reports what each stage did by
reading the structured log events back.

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

Two components: a flat monthly floor, and a per-alert charge dominated by Bedrock.

**Fixed, at zero alerts — about $3.10/month:**

| Item | Monthly |
| --- | --- |
| Secrets Manager — 4 secrets × $0.40 | $1.60 |
| CloudWatch alarms — 6 × $0.10 | $0.60 |
| CloudWatch custom metrics — 3 × $0.30 | $0.90 |
| Dashboard (1; first 3 are free) | $0.00 |
| S3 artifacts, SQS, EventBridge, SNS, idle Lambda | <$0.02 |

**Per alert — about $0.025**, of which Bedrock is ~97%. A fully-enriched alert
sends roughly 5k input tokens (alert payload plus six telemetry sources) and
generates ~600 output. Lambda is ~$0.0001 per invocation at 512 MB arm64; the
Logs Insights query is ~$0.0003; everything else is noise.

| Alerts / month | Monthly total |
| --- | --- |
| 100 | ~$6 |
| 500 | ~$16 |
| 1,000 | ~$28 |
| 5,000 | ~$128 |

Three things worth knowing:

- **Prompt caching does not apply.** The system prompt is ~600 tokens, under the
  1,024-token minimum cacheable prefix for Sonnet-tier models — the cache is
  silently never written. Enlarging the prompt to cross that line would cost more
  than it saves at these volumes.
- **Resolved alerts are dropped before any model call**, so recovery notices are
  free. In a flapping-alert incident that is most of the traffic.
- **`lambda_reserved_concurrency` is currently `-1`** (no reservation) because
  this account's total Lambda concurrency quota is 10, which already caps
  parallel Bedrock calls harder than a reservation would. If that quota is
  raised, set the reservation or a flapping rule can fan out across the new
  headroom — see `docs/BOOTSTRAP.md`.

Measure rather than trust the estimate: every summary logs its real token counts.

```text
fields @timestamp, input_tokens, output_tokens
| filter event = "bedrock_invoke_succeeded"
| stats sum(input_tokens) as in, sum(output_tokens) as out, count(*) as calls by bin(1d)
```

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

First deployment into a fresh account: [`docs/BOOTSTRAP.md`](docs/BOOTSTRAP.md).

Wiring an existing workload in as a signal source — CloudWatch alarms or
direct `PutEvents` from your application: [`docs/INTEGRATIONS.md`](docs/INTEGRATIONS.md).

See [`docs/RUNBOOK.md`](docs/RUNBOOK.md) for operating and troubleshooting the
deployed system.
