#!/usr/bin/env bash
#
# End-to-end smoke test: EventBridge -> Lambda -> enrichment -> Bedrock -> Slack.
#
# Publishes one synthetic firing alert to the bus and reports what each stage of
# the handler did, by reading the structured log events back.
#
# Usage:
#   scripts/smoke-test.sh                 # firing alert (spends a Bedrock call)
#   scripts/smoke-test.sh --resolved      # parse/route only, no model call
#   scripts/smoke-test.sh --cloudwatch    # CloudWatch alarm payload shape
#   scripts/smoke-test.sh --no-cold-start # skip the container drain
#
set -euo pipefail

ENVIRONMENT="${ENVIRONMENT:-dev}"
PROJECT="${PROJECT:-ai-incident-summarizer}"
PREFIX="${PROJECT}-${ENVIRONMENT}"
FUNC="${PREFIX}-summarizer"
BUS="${PREFIX}-alerts"
LOG_GROUP="/aws/lambda/${FUNC}"

STATUS="firing"
COLD_START=1
SHAPE="grafana"
for arg in "$@"; do
  case "$arg" in
    --resolved)      STATUS="resolved" ;;
    --cloudwatch)    SHAPE="cloudwatch" ;;
    --no-cold-start) COLD_START=0 ;;
    -h|--help)       sed -n '2,16p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

ALERT="smoke-$(date -u +%Y%m%dT%H%M%SZ)"
say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. Cold start
#
# get_secret is cached for the container's lifetime, so a warm container keeps
# serving whatever the secret held when it started. Draining guarantees the test
# exercises the *current* secret values. Toggling reserved concurrency returns to
# the Terraform-managed state, unlike a --description bump, which leaves drift.
# ---------------------------------------------------------------------------
if [ "$COLD_START" -eq 1 ]; then
  say "Draining containers so secrets are re-read"
  aws lambda put-function-concurrency --function-name "$FUNC" \
    --reserved-concurrent-executions 0 >/dev/null
  aws lambda delete-function-concurrency --function-name "$FUNC" >/dev/null
  echo "  done"
fi

# ---------------------------------------------------------------------------
# 2. Publish
# ---------------------------------------------------------------------------
say "Publishing a ${STATUS} alert to ${BUS}"
DETAIL=$(python3 - "$ALERT" "$STATUS" "$SHAPE" <<'PY'
import json, sys
name, status, shape = sys.argv[1], sys.argv[2], sys.argv[3]

if shape == "cloudwatch":
    # The shape AWS emits for "CloudWatch Alarm State Change". Exercised here
    # because its `state` is an object, not a string — a difference that once
    # caused every alarm to be silently dropped as resolved.
    print(json.dumps({
        "alarmName": name,
        "state": {
            "value": "ALARM" if status == "firing" else "OK",
            "reason": "Threshold Crossed: 3 datapoints above 10.0 (synthetic)",
        },
        "configuration": {
            "description": "Synthetic alarm from scripts/smoke-test.sh",
            "metrics": [{"metricStat": {"metric": {"dimensions": {
                "service": "checkout-api", "InstanceId": "i-0synthetic",
            }}}}],
        },
    }))
else:
    print(json.dumps({
        "alerts": [{
            "status": status,
            "labels": {"alertname": name, "severity": "critical", "service": "checkout-api"},
            "annotations": {"summary": "Synthetic alert from scripts/smoke-test.sh"},
            "fingerprint": name,
        }],
        "status": status,
    }))
PY
)
ENTRY=$(python3 - "$BUS" "$DETAIL" <<'PY'
import json, sys
print(json.dumps([{
    "EventBusName": sys.argv[1],
    "Source": "grafana.alerts",
    "DetailType": "GrafanaAlert",
    "Detail": sys.argv[2],
}]))
PY
)
FAILED=$(aws events put-events --entries "$ENTRY" --query FailedEntryCount --output text)
[ "$FAILED" = "0" ] || { echo "put-events rejected the entry" >&2; exit 1; }
echo "  accepted (alertname=${ALERT})"

# ---------------------------------------------------------------------------
# 3. Wait for the handler to finish
# ---------------------------------------------------------------------------
say "Waiting for the handler"
START_MS=$(( ($(date +%s) - 120) * 1000 ))
for _ in $(seq 1 30); do
  DONE=$(aws logs filter-log-events --log-group-name "$LOG_GROUP" \
    --start-time "$START_MS" --filter-pattern '"invocation_completed"' \
    --query 'length(events)' --output text 2>/dev/null || echo 0)
  [ "$DONE" != "0" ] && break
  sleep 5
done
[ "${DONE:-0}" != "0" ] || { echo "  no invocation_completed within 150s" >&2; exit 1; }

# ---------------------------------------------------------------------------
# 4. Report
# ---------------------------------------------------------------------------
say "Handler trace"
aws logs filter-log-events --log-group-name "$LOG_GROUP" --start-time "$START_MS" \
  --query 'events[].message' --output text 2>/dev/null \
 | tr '\t' '\n' | python3 -c '
import json, sys
KEEP = ("alerts_parsed", "context_gathered", "bedrock_invoke_succeeded",
        "bedrock_invoke_failed", "summarization_failed", "slack_post_succeeded",
        "slack_post_failed", "invocation_completed")
ok = True
for line in sys.stdin:
    line = line.strip()
    if not line.startswith("{"):
        continue
    try:
        r = json.loads(line)
    except ValueError:
        continue
    e = r.get("event")
    if e not in KEEP:
        continue
    mark = {"ERROR": "FAIL", "WARNING": "warn"}.get(r.get("level"), " ok ")
    if mark == "FAIL":
        ok = False
    extra = {k: v for k, v in r.items()
             if k not in ("level", "event", "message", "logger")}
    print(f"  [{mark}] {e:26s} {json.dumps(extra, default=str)[:150]}")
sys.exit(0 if ok else 1)
' && RESULT=0 || RESULT=1

say "Result"
if [ "$RESULT" -eq 0 ]; then
  echo "  PASS - no errors in the pipeline"
else
  echo "  FAIL - see the [FAIL] lines above"
fi
exit "$RESULT"
