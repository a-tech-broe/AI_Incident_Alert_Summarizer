"""CloudWatch context collection: alarm state, ECS metrics and error logs.

Logs Insights is used rather than FilterLogEvents because it can aggregate — a
count of distinct error signatures is far more useful in a prompt than fifty
near-identical stack traces, and it keeps the payload well inside the model's
useful attention span.
"""

from __future__ import annotations

import contextlib
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

from config import Deadline, get_client, get_config, log_event

_MAX_LOG_PATTERNS = 12
_INSIGHTS_POLL_INTERVAL = 1.0

# Errors are what a responder greps for first. Broad enough to catch most
# application conventions, anchored enough not to match every INFO line.
_ERROR_QUERY = """
fields @timestamp, @message
| filter @message like /(?i)(error|exception|fatal|panic|traceback|timeout|refused)/
| stats count(*) as occurrences by @message
| sort occurrences desc
| limit {limit}
"""


def collect_alarms(alert_name: str, service: str | None, deadline: Deadline) -> dict[str, Any]:
    """Find CloudWatch alarms currently in ALARM that relate to this incident.

    Neighbouring alarms are the cheapest available signal for blast radius: one
    alarm is a symptom, five across the same service is an outage.
    """
    if deadline.expired():
        return {"available": False, "reason": "Skipped: invocation deadline reached"}

    try:
        client = get_client("cloudwatch")
        response = client.describe_alarms(StateValue="ALARM", MaxRecords=100)

        alarms = []
        for alarm in response.get("MetricAlarms", []):
            alarms.append(
                {
                    "name": alarm.get("AlarmName"),
                    "metric": alarm.get("MetricName"),
                    "namespace": alarm.get("Namespace"),
                    "reason": alarm.get("StateReason"),
                    "since": alarm.get("StateUpdatedTimestamp"),
                    "related": _is_related(alarm, alert_name, service),
                }
            )

        related = [a for a in alarms if a["related"]]
        return {
            "available": True,
            "total_in_alarm": len(alarms),
            "related_alarms": related[:10],
            "blast_radius_note": _blast_radius_note(len(alarms), len(related)),
        }

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        log_event(logging.WARNING, "cloudwatch_alarms_failed", "Alarm lookup failed", error_code=code)
        return {"available": False, "reason": f"CloudWatch API error: {code}"}


def collect_metrics(cluster: str | None, service: str | None, deadline: Deadline) -> dict[str, Any]:
    """Pull ECS CPU and memory utilization over the lookback window."""
    if not (cluster and service):
        return {"available": False, "reason": "No cluster/service labels to scope metrics to"}
    if deadline.expired():
        return {"available": False, "reason": "Skipped: invocation deadline reached"}

    config = get_config()
    end = datetime.now(UTC)
    start = end - timedelta(minutes=config.lookback_minutes)

    dimensions = [
        {"Name": "ClusterName", "Value": cluster},
        {"Name": "ServiceName", "Value": service},
    ]

    queries = [
        _metric_query("cpu", "CPUUtilization", dimensions, "Average"),
        _metric_query("cpu_max", "CPUUtilization", dimensions, "Maximum"),
        _metric_query("mem", "MemoryUtilization", dimensions, "Average"),
        _metric_query("mem_max", "MemoryUtilization", dimensions, "Maximum"),
    ]

    try:
        response = get_client("cloudwatch").get_metric_data(
            MetricDataQueries=queries,
            StartTime=start,
            EndTime=end,
            ScanBy="TimestampDescending",
        )

        series = {r["Id"]: r.get("Values", []) for r in response.get("MetricDataResults", [])}
        return {
            "available": True,
            "window_minutes": config.lookback_minutes,
            "cpu_utilization": _describe_series(series.get("cpu", []), series.get("cpu_max", [])),
            "memory_utilization": _describe_series(series.get("mem", []), series.get("mem_max", [])),
        }

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        log_event(logging.WARNING, "cloudwatch_metrics_failed", "Metric fetch failed", error_code=code)
        return {"available": False, "reason": f"CloudWatch API error: {code}"}


def collect_logs(service: str | None, deadline: Deadline) -> dict[str, Any]:
    """Aggregate distinct error signatures from the service's log groups."""
    if not service:
        return {"available": False, "reason": "No service label to locate log groups with"}

    config = get_config()

    # Insights queries are the slowest enrichment step. Only start one if there
    # is a realistic chance of it finishing before the Bedrock reserve.
    if deadline.remaining_seconds() < 8:
        return {"available": False, "reason": "Skipped: insufficient time before invocation deadline"}

    try:
        logs = get_client("logs")
        log_groups = _find_log_groups(logs, service)
        if not log_groups:
            return {"available": False, "reason": f"No log groups matched '{service}'"}

        end = int(time.time())
        start = end - config.lookback_minutes * 60

        query_id = logs.start_query(
            logGroupNames=log_groups[:5],  # Insights caps the group count per query
            startTime=start,
            endTime=end,
            queryString=_ERROR_QUERY.format(limit=_MAX_LOG_PATTERNS),
        )["queryId"]

        results = _await_query(logs, query_id, deadline)
        if results is None:
            return {
                "available": False,
                "reason": "Logs Insights query did not complete before the deadline",
                "log_groups_searched": log_groups[:5],
            }

        patterns = []
        for row in results:
            fields = {f["field"]: f["value"] for f in row}
            message = fields.get("@message", "").strip()
            patterns.append(
                {
                    # Long stack traces crowd out every other signal in the
                    # prompt; the first lines carry the diagnostic content.
                    "message": message[:500] + ("…" if len(message) > 500 else ""),
                    "occurrences": int(fields.get("occurrences", 0) or 0),
                }
            )

        return {
            "available": True,
            "window_minutes": config.lookback_minutes,
            "log_groups_searched": log_groups[:5],
            "distinct_error_patterns": len(patterns),
            "total_error_events": sum(p["occurrences"] for p in patterns),
            "top_errors": patterns,
        }

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        log_event(logging.WARNING, "cloudwatch_logs_failed", "Log query failed", error_code=code)
        return {"available": False, "reason": f"CloudWatch Logs API error: {code}"}
    except Exception as exc:  # noqa: BLE001 - enrichment must never break the summary
        log_event(logging.WARNING, "cloudwatch_logs_failed", "Unexpected log query error", error=str(exc))
        return {"available": False, "reason": f"Unexpected error: {exc}"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _metric_query(query_id: str, metric: str, dimensions: list[dict[str, str]], stat: str) -> dict[str, Any]:
    return {
        "Id": query_id,
        "MetricStat": {
            "Metric": {"Namespace": "AWS/ECS", "MetricName": metric, "Dimensions": dimensions},
            "Period": 300,
            "Stat": stat,
        },
        "ReturnData": True,
    }


def _describe_series(averages: list[float], maximums: list[float]) -> dict[str, Any]:
    """Reduce a time series to the few numbers that change a diagnosis.

    Latest vs. window-average is what distinguishes "it just spiked" from "it has
    been saturated for an hour", and that distinction changes the response.
    """
    if not averages:
        return {"available": False}

    latest = averages[0]  # ScanBy=TimestampDescending
    window_avg = sum(averages) / len(averages)
    peak = max(maximums) if maximums else max(averages)

    return {
        "available": True,
        "latest_percent": round(latest, 1),
        "window_average_percent": round(window_avg, 1),
        "peak_percent": round(peak, 1),
        "trend": _trend(averages),
    }


def _trend(values: list[float]) -> str:
    if len(values) < 4:
        return "insufficient data"

    # Values arrive newest-first; compare the recent quarter against the rest.
    split = max(1, len(values) // 4)
    recent = sum(values[:split]) / split
    baseline = sum(values[split:]) / (len(values) - split)

    if baseline == 0:
        return "flat"

    change = (recent - baseline) / baseline
    if change > 0.2:
        return f"rising ({change:+.0%} vs window baseline)"
    if change < -0.2:
        return f"falling ({change:+.0%} vs window baseline)"
    return "flat"


def _find_log_groups(client: Any, service: str) -> list[str]:
    """Locate log groups belonging to a service by name convention."""
    seen: list[str] = []
    for prefix in (f"/ecs/{service}", f"/aws/ecs/{service}", f"/{service}", service):
        try:
            response = client.describe_log_groups(logGroupNamePrefix=prefix, limit=10)
        except ClientError:
            continue
        for group in response.get("logGroups", []):
            name = group["logGroupName"]
            if name not in seen:
                seen.append(name)
        if seen:
            break
    return seen


def _await_query(client: Any, query_id: str, deadline: Deadline) -> list[Any] | None:
    """Poll an Insights query, abandoning it if the deadline arrives first."""
    while not deadline.expired():
        response = client.get_query_results(queryId=query_id)
        status = response.get("status")

        if status == "Complete":
            return response.get("results", [])
        if status in ("Failed", "Cancelled", "Timeout"):
            log_event(
                logging.WARNING,
                "logs_insights_query_failed",
                "Logs Insights query ended without results",
                status=status,
            )
            return None

        time.sleep(_INSIGHTS_POLL_INTERVAL)

    # Leaving a query running would keep billing against the account for no
    # benefit, since the result can no longer be used. A failure to cancel is
    # not worth surfacing — the query may simply have completed in the interim.
    with contextlib.suppress(ClientError):
        client.stop_query(queryId=query_id)
    return None


def _is_related(alarm: dict[str, Any], alert_name: str, service: str | None) -> bool:
    haystack = " ".join(
        filter(
            None,
            [
                alarm.get("AlarmName", ""),
                alarm.get("AlarmDescription", ""),
                *[d.get("Value", "") for d in alarm.get("Dimensions", [])],
            ],
        )
    ).lower()

    if service and service.lower() in haystack:
        return True

    # Fall back to overlapping words in the alert name, ignoring filler tokens
    # that would match nearly every alarm in the account.
    tokens = {t for t in alert_name.lower().replace("-", " ").replace("_", " ").split() if len(t) > 3}
    tokens -= {"high", "alert", "alarm", "error", "rate", "usage"}
    return any(token in haystack for token in tokens)


def _blast_radius_note(total: int, related: int) -> str:
    if total <= 1:
        return "Isolated: this is the only alarm currently firing in the account."
    if related > 1:
        return f"{related} related alarms are firing — likely a single underlying fault."
    return f"{total} alarms are firing account-wide, but only this one matches the affected service."
