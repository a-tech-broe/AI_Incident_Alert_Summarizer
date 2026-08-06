# Sending signals from an existing application

Two ways to feed the summarizer from a workload running on EC2. They are not
alternatives — most setups want both, because they answer different questions.

| | What it catches | App changes | Effort |
| --- | --- | --- | --- |
| **A. CloudWatch alarms** | Infrastructure symptoms: CPU, memory, ALB 5xx, RDS connections, disk | None | One Terraform toggle |
| **B. Direct `PutEvents`** | Application semantics: circuit breaker opened, payment provider down, queue backed up | A few lines | IAM + a helper |

Path A tells you *something is wrong*. Path B tells you *what your code thinks
is wrong* — which is usually the more useful summary, because the app knows the
service name, the severity, and the business impact.

Both land on the same bus and go through the same handler.

---

## Path A — CloudWatch alarms, no application changes

AWS publishes alarm state changes onto the account's **default** event bus, and
a rule cannot route from the default bus to this stack's rule directly. The
module can republish them onto the alerts bus for you.

In `terraform/terraform.tfvars`:

```hcl
forward_cloudwatch_alarms = true

# Strongly recommended when the account hosts other projects — without a prefix
# every alarm in the account is forwarded, including ones you do not own.
forwarded_alarm_name_prefixes = ["banking-platform-"]
```

Then `terraform apply`. That creates a rule on the default bus, an IAM role
permitting `events:PutEvents` onto the alerts bus, and adds `aws.cloudwatch` to
the sources the summarizer rule matches.

**Alarms need no changes** — any existing alarm is picked up automatically once
its name matches a configured prefix.

### Make the summaries useful

The handler reads the alarm's metric **dimensions** to find the affected
workload. An alarm whose dimensions include a recognizable service name gets ECS
lookups, service-scoped log queries, and correlated metrics. One without gets a
summary built from the alarm text alone.

Recognized keys: `service`, `service_name`, `app`, `application`, `ecs_service`,
`job` for the service; `cluster`, `ecs_cluster`, `cluster_name` for the cluster.

If your metrics are dimensioned by `InstanceId` alone, consider publishing a
custom metric dimensioned by service as well — it is the single highest-leverage
change for summary quality.

Alarm **description** is passed to the model, so it is worth writing:

```bash
aws cloudwatch put-metric-alarm --alarm-name banking-platform-api-5xx \
  --alarm-description "ALB 5xx above 10/min. Usually a bad deploy or an unhealthy target." \
  ...
```

### What maps to what

| CloudWatch | Becomes |
| --- | --- |
| `alarmName` | Alert name |
| `state.value = ALARM` | Firing |
| `state.value = OK` / `INSUFFICIENT_DATA` | Not firing — dropped before any model call |
| `state.reason` | Alert summary |
| `configuration.description` | Alert description |
| Metric dimensions | Labels, and the service/cluster lookup |

`INSUFFICIENT_DATA` is deliberately not treated as firing. It usually means a
metric stopped reporting, which is noisy enough to deserve its own alarm rather
than paging through this one.

---

## Path B — publish directly from the application

Best for conditions only your code can detect. The app publishes one event; the
summarizer does the rest.

### 1. Grant the instance role permission

Attach to the EC2 instance profile (or ECS task role, or whatever identity the
app runs as):

```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Sid": "PublishIncidentAlerts",
    "Effect": "Allow",
    "Action": "events:PutEvents",
    "Resource": "arn:aws:events:us-east-1:694992586025:event-bus/ai-incident-summarizer-dev-alerts"
  }]
}
```

This is the *only* permission the app needs. It cannot read the bus, invoke the
Lambda, or reach Bedrock — a compromised app can raise a false alert and nothing
more.

### 2. Publish

```python
import boto3, json, socket, os

_events = boto3.client("events")
BUS = "ai-incident-summarizer-dev-alerts"


def raise_alert(name, summary, *, severity="critical", service=None, **labels):
    """Publish one alert. Never let telemetry break the caller."""
    payload = {
        "alerts": [{
            "status": "firing",
            "labels": {
                "alertname": name,
                "severity": severity,          # critical | high | medium | low
                "service": service or os.getenv("SERVICE_NAME", "unknown"),
                "instance": socket.gethostname(),
                **{k: str(v) for k, v in labels.items()},
            },
            "annotations": {
                "summary": summary,
                # A runbook link is rendered as a button in Slack.
                "runbook_url": f"https://runbooks.example.com/{name}",
            },
        }],
        "status": "firing",
    }
    try:
        _events.put_events(Entries=[{
            "EventBusName": BUS,
            "Source": "grafana.alerts",   # must match terraform var.event_sources
            "DetailType": "ApplicationAlert",
            "Detail": json.dumps(payload),
        }])
    except Exception:
        # An alerting path that can take down the app is worse than no alerting.
        logging.exception("failed to publish alert")
```

Call it where your code already knows something is wrong:

```python
if breaker.state == "open":
    raise_alert(
        "PaymentProviderCircuitOpen",
        f"Circuit breaker opened after {breaker.failures} consecutive failures.",
        service="checkout-api",
        provider="stripe",
    )
```

### Two things that matter

**`Source` must be `grafana.alerts`** (or whatever is in `var.event_sources`) or
the rule will not match. `PutEvents` returns success for an event nobody
consumes, so a wrong source fails silently. Verify with:

```bash
aws cloudwatch get-metric-statistics --namespace AWS/Events --metric-name TriggeredRules \
  --dimensions Name=RuleName,Value=ai-incident-summarizer-dev-alert-to-summarizer \
               Name=EventBusName,Value=ai-incident-summarizer-dev-alerts \
  --start-time "$(date -u -d '1 hour ago' +%FT%T)" --end-time "$(date -u +%FT%T)" \
  --period 3600 --statistics Sum
```

Both dimensions are required. Omitting `EventBusName` returns no datapoints and
looks identical to "the rule never fired".

**Deduplicate before you publish.** A retry loop calling `raise_alert` on every
failure will publish thousands of events and spend a model call on each. Raise
on the *transition* into a bad state, not on every occurrence — a flapping rule
is the most likely way to run up a bill here.

---

## Choosing severity

It is passed to the model as the alert's own claim, and the summary reports the
model's independent assessment alongside it. Disagreement between the two is
signal — it usually means the alert threshold is mistuned.

| Severity | Use for |
| --- | --- |
| `critical` | User-visible outage or data loss risk |
| `high` | Degraded service, or a dependency down with a working fallback |
| `medium` | Elevated errors not yet user-visible |
| `low` | Informational; worth recording, not worth waking someone |

---

## Verifying either path

```bash
./scripts/smoke-test.sh              # synthetic alert through the whole chain
aws logs tail /aws/lambda/ai-incident-summarizer-dev-summarizer --follow --format short
```

Look for `context_gathered` and check `sources_available`. If it is short, the
alert is missing the labels that unlock enrichment — see the service/cluster
keys above. That single field is the difference between a summary built from
real telemetry and one built from the alert text alone.
