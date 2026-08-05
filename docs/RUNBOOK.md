# Runbook — AI Incident Summarizer

Operating and troubleshooting the deployed system. For setup and architecture,
see the [README](../README.md).

Names below assume `ai-incident-summarizer-dev`; substitute your prefix.

---

## Health check

```bash
FUNC=ai-incident-summarizer-dev-summarizer

# Is the function healthy and pointed at real code?
aws lambda get-function --function-name "$FUNC" \
  --query 'Configuration.[State,LastUpdateStatus,Version,CodeSize]'

# Anything stuck in the dead-letter queue?
aws sqs get-queue-attributes \
  --queue-url "$(terraform -chdir=terraform output -raw dead_letter_queue_url)" \
  --attribute-names ApproximateNumberOfMessagesVisible

# Any alarms firing?
aws cloudwatch describe-alarms --alarm-name-prefix ai-incident-summarizer-dev \
  --state-value ALARM --query 'MetricAlarms[].AlarmName'
```

A `CodeSize` near 300 bytes means the function is still running the Terraform
bootstrap placeholder and no real deployment has landed.

---

## Symptom: no Slack message arrived

Work down the pipeline — each step narrows where it broke.

**1. Did the Lambda run at all?**

```bash
aws logs tail /aws/lambda/ai-incident-summarizer-dev-summarizer --since 30m --follow
```

No log entries means the alert never reached the function. Check the Function
URL is reachable and Grafana's contact point is configured, or that the producer
is publishing to the right EventBridge bus with a matching `source`.

**2. Was the payload understood?**

```
filter event = "alert_parse_failed"
```

The log record carries `event_keys` — the top-level keys of the payload, without
its contents. Compare against what `alert.py` expects.

**3. Was it dropped as resolved?**

```
filter event = "alerts_parsed" | fields firing_count, resolved_count
```

Resolution notices are skipped deliberately.

**4. Did Bedrock fail?**

```
filter event = "bedrock_invoke_failed" | fields error_code, model_id
```

See the Bedrock section below for the common error codes.

**5. Did Slack reject the message?**

```
filter event = "slack_post_failed" | fields status, slack_response
```

Slack returns the reason in plain text, and it usually names the fix directly:
`invalid_blocks`, `no_service`, `channel_not_found`.

---

## Symptom: summaries are vague or unhelpful

Check what context was actually gathered:

```
filter event = "context_gathered" | fields alert_name, sources_available, sources_unavailable
```

If `sources_available` is consistently short, the model is reasoning from the
alert payload alone and cannot do better. Common causes:

| Missing source | Usual cause |
| --- | --- |
| `ecs_service` | The alert carries no `service` label, or the label does not match the ECS service name |
| `cloudwatch_metrics` | Needs *both* `cluster` and `service` labels to scope the query |
| `cloudwatch_logs` | Log groups do not match `/ecs/<service>`, `/aws/ecs/<service>` or `/<service>` |
| `grafana` / `splunk` | `*_base_url` unset, or the token secret still holds the placeholder |

Most of these are fixed in the Grafana alert rule rather than in this codebase:
adding `service` and `cluster` labels to the rule is the single highest-leverage
change available.

If sources are available but the summary is still weak, check whether the
deadline is cutting enrichment short:

```
filter event = "context_gathered" | fields duration_ms
filter message like /deadline/
```

Raise `lambda_timeout`, or reduce `context_lookback_minutes` so the Logs
Insights and Splunk queries return faster.

---

## Symptom: alerts land in the dead-letter queue

The DLQ receives events EventBridge could not deliver, plus async invocations
that exhausted their retries.

```bash
QUEUE=$(terraform -chdir=terraform output -raw dead_letter_queue_url)

# Inspect without consuming
aws sqs receive-message --queue-url "$QUEUE" --max-number-of-messages 10 \
  --visibility-timeout 0 --message-attribute-names All
```

The message attributes carry `ERROR_CODE` and `ERROR_MESSAGE` from EventBridge.
Usual causes are the Lambda being throttled (raise
`lambda_reserved_concurrency`) or failing at init (check the log group for an
import error after a bad deploy).

After fixing the cause, replay from the EventBridge archive rather than
hand-crafting events:

```bash
aws events start-replay \
  --replay-name dlq-replay-$(date +%s) \
  --event-source-arn "$(terraform -chdir=terraform output -raw event_bus_arn 2>/dev/null)" \
  --event-start-time <start> --event-end-time <end> \
  --destination '{"Arn":"<bus-arn>","FilterArns":["<rule-arn>"]}'
```

Purge the queue once the replay succeeds, so the DLQ alarm clears.

---

## Bedrock errors

| Error code | Meaning | Fix |
| --- | --- | --- |
| `AccessDeniedException` | Model access not granted | Request access in the Bedrock console for this account **and region** |
| `ValidationException` | Bad model ID | Cross-region profiles need the geography prefix (`us.`) and must exist in your region |
| `ThrottlingException` | Account quota exceeded | Lower `lambda_reserved_concurrency`, or request a quota increase |
| `ModelTimeoutException` | Generation exceeded the read timeout | Lower `bedrock_max_tokens`, or raise the timeout in `config.py` |

Model access is the one that bites on a fresh account: everything applies
cleanly, and every invocation fails.

Watch token spend:

```
filter event = "bedrock_invoke_succeeded"
| stats sum(input_tokens) as in, sum(output_tokens) as out, count(*) as calls by bin(1h)
```

---

## Rotating a secret

```bash
aws secretsmanager put-secret-value \
  --secret-id ai-incident-summarizer-dev/slack-webhook \
  --secret-string '{"value":"https://hooks.slack.com/services/NEW"}'
```

Values are stripped of surrounding whitespace on read — a pasted leading space
would otherwise produce a confusing `InvalidURL` / "control characters" failure
rather than an obvious typo.

Terraform ignores changes to secret values, so a later `apply` will not revert
them.

### Forcing the new value to take effect

`get_secret` is cached for the life of the execution container, so a warm
container keeps serving the old value. Containers recycle on their own after
roughly 5–15 minutes idle; if you can wait, do nothing.

To force it, toggle reserved concurrency down and back:

```bash
aws lambda put-function-concurrency --function-name "$FUNC" \
  --reserved-concurrent-executions 0            # drains all containers
aws lambda delete-function-concurrency --function-name "$FUNC"   # back to unreserved
```

This ends in exactly the state Terraform expects (`lambda_reserved_concurrency
= -1`), so it leaves no drift. Invocations are rejected for the few seconds
between the two commands.

**Do not use `update-function-configuration --description` for this.** It works,
but `description` is Terraform-managed, so the change shows up as a pending
update on every later plan until an apply reverts it. Any Terraform-managed
attribute has the same problem — reserved concurrency is the exception only
because deleting the override restores the managed value.

---

## Rolling back

The `live` alias only moves after the deploy pipeline's smoke test passes, so a
failed deploy leaves it on the previous version. To roll back an *apparently*
successful deploy:

```bash
aws lambda list-versions-by-function --function-name "$FUNC" \
  --query 'Versions[].[Version,LastModified]' --output table

aws lambda update-alias --function-name "$FUNC" \
  --name live --function-version <previous>
```

Note that event sources invoke the unqualified function, not the alias — the
alias is a rollback marker and a smoke-test target. To make rollback take effect
for live traffic, either redeploy the previous package from S3 (versioning is
enabled on the artifacts bucket) or point the EventBridge target at the alias
ARN.

---

## Pausing the pipeline

To stop summarization without tearing anything down:

```bash
# Stop EventBridge-routed alerts
aws events disable-rule --name ai-incident-summarizer-dev-alert-to-summarizer \
  --event-bus-name ai-incident-summarizer-dev-alerts

# Stop all invocations, including the Function URL
aws lambda put-function-concurrency --function-name "$FUNC" \
  --reserved-concurrent-executions 0
```

Reverse with `enable-rule` and by restoring `lambda_reserved_concurrency`.
Setting concurrency to zero means alerts are *rejected*, not queued — they will
accumulate in the DLQ.

---

## Testing without an incident

```bash
cat > /tmp/test-alert.json <<'JSON'
{
  "alerts": [{
    "status": "firing",
    "labels": {
      "alertname": "HighErrorRate",
      "severity": "critical",
      "service": "checkout-api",
      "cluster": "prod-cluster"
    },
    "annotations": {"summary": "Error rate above 5% for 10 minutes"},
    "startsAt": "2026-08-04T10:00:00Z",
    "fingerprint": "manualtest"
  }],
  "status": "firing"
}
JSON

aws lambda invoke --function-name "$FUNC" \
  --payload fileb:///tmp/test-alert.json /tmp/response.json
cat /tmp/response.json | jq
```

This posts a real message to Slack and spends a real Bedrock call. Use
`"status": "resolved"` to exercise parsing and routing without either.

---

## Cost controls

| Control | Where | Effect |
| --- | --- | --- |
| `lambda_reserved_concurrency` | tfvars | Caps parallel Bedrock calls during an alert storm |
| `bedrock_max_tokens` | tfvars | Caps output tokens per summary |
| Resolved-alert filter | `app.py` | No model call for recovery notices |
| `context_lookback_minutes` | tfvars | Smaller windows mean cheaper Insights and Splunk queries |

The most likely runaway is a flapping alert rule firing repeatedly. The
`lambda-errors` and `bedrock-failures` alarms will not catch that — invocations
are succeeding. Watch invocation count on the dashboard, and fix the rule
upstream.
