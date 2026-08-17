"""AI incident summarizer — Lambda entry point.

Pipeline per alert:

    normalize -> gather context (concurrently) -> summarize -> notify

Two properties drive the structure:

**Enrichment never blocks notification.** Every collector returns a status dict
instead of raising, and the whole gather stage runs under a deadline that
reserves time for Bedrock. A degraded Splunk must cost detail, not the page.

**Delivery is the last thing to fail.** If Bedrock errors or returns
unparseable output, the raw alert still reaches Slack. The failure mode of an
incident-response tool must be "less useful", never "silent".
"""

from __future__ import annotations

import hmac
import json
import logging
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import Any

import alert as alert_parser
import cloudwatch
import ecs
import grafana
import prompt
import slack
import splunk
from config import Deadline, SecretNotConfigured, get_config, get_secret, log_event

# Bedrock generation is the one step that cannot be skipped or degraded, so it
# gets a fixed slice of the timeout that enrichment may not encroach on.
_BEDROCK_RESERVE_SECONDS = 22.0

# Slack delivery after generation. Small, but it is the step that turns a
# successful summary into a delivered one, so it is budgeted rather than assumed.
_DELIVERY_RESERVE_SECONDS = 5.0

# An alert is only worth starting if generation *and* delivery still fit.
_ALERT_BUDGET_SECONDS = _BEDROCK_RESERVE_SECONDS + _DELIVERY_RESERVE_SECONDS

# A group's alerts are summarized concurrently: each needs its own Bedrock call,
# and serially they cannot fit in the timeout. Bounded rather than unbounded —
# every worker holds an in-flight Bedrock call, and this account's model
# throughput is the scarcer resource. Raise it only alongside the quota.
_MAX_CONCURRENT_ALERTS = 3

_DEADLINE_SKIP_ERROR = "invocation deadline reached before summarization"


def lambda_handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    started = time.monotonic()
    config = get_config()

    log_event(
        logging.INFO,
        "invocation_started",
        "Received event",
        request_id=getattr(context, "aws_request_id", None),
        environment=config.environment,
    )

    # Function URL requests are unauthenticated at the AWS layer, so the shared
    # secret check happens here, before any payload parsing.
    if _is_function_url_request(event):
        rejection = _reject_unauthenticated(event)
        if rejection is not None:
            return rejection

    try:
        alerts, source_path = alert_parser.parse(event)
    except alert_parser.UnparseableAlert as exc:
        log_event(
            logging.ERROR,
            "alert_parse_failed",
            "Could not parse inbound payload",
            error=str(exc),
            # The payload shape, not its content — enough to debug a misrouted
            # producer without copying alert data into the log.
            event_keys=sorted(event.keys()) if isinstance(event, dict) else None,
        )
        return _response(400, {"error": "Unrecognized alert payload"})

    firing = [a for a in alerts if a.is_firing]
    resolved = len(alerts) - len(firing)

    log_event(
        logging.INFO,
        "alerts_parsed",
        "Normalized inbound alerts",
        source_path=source_path,
        firing_count=len(firing),
        resolved_count=resolved,
    )

    # Resolution notices carry no diagnostic value and would spend a Bedrock
    # call restating that something recovered.
    if not firing:
        return _response(200, {"processed": 0, "skipped_resolved": resolved})

    deadline = Deadline(context, reserve_seconds=_BEDROCK_RESERVE_SECONDS)
    results = _process_alerts(firing, deadline)

    duration_ms = round((time.monotonic() - started) * 1000)
    delivered = sum(1 for r in results if r["delivered"])

    log_event(
        logging.INFO,
        "invocation_completed",
        "Finished processing alerts",
        processed=len(results),
        delivered=delivered,
        failed=len(results) - delivered,
        duration_ms=duration_ms,
    )

    # A 200 with a failure count, rather than a raised exception: EventBridge
    # would otherwise retry the whole batch and re-deliver summaries that
    # already reached Slack.
    return _response(
        200,
        {
            "processed": len(results),
            "delivered": delivered,
            "skipped_resolved": resolved,
            "duration_ms": duration_ms,
            "results": results,
        },
    )


# ---------------------------------------------------------------------------
# Per-alert pipeline
# ---------------------------------------------------------------------------


def _process_alerts(firing: list[alert_parser.Alert], deadline: Deadline) -> list[dict[str, Any]]:
    """Summarize every firing alert in the group, concurrently and in order.

    A Grafana group carries one entry per firing instance, and each entry needs
    its own enrichment and its own Bedrock call. Run serially, the second alert
    starts with the first one's time already spent and the third is killed
    mid-generation — and a killed invocation is not a return value, so it
    bypasses the "200 with a failure count" contract below and EventBridge
    retries the whole batch, re-posting summaries that already reached Slack.

    Concurrency turns the sum of those costs into the maximum of them. The
    per-alert budget check in `_process_alert` covers what still does not fit.
    """
    cache = _ContextCache()

    if len(firing) == 1:
        return [_process_alert_safely(firing[0], deadline, cache)]

    workers = min(len(firing), _MAX_CONCURRENT_ALERTS)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_process_alert_safely, a, deadline, cache) for a in firing]
        # Indexed rather than as-completed: the response should list alerts in
        # the order the producer sent them, not the order they finished.
        return [future.result() for future in futures]


def _process_alert_safely(
    alert: alert_parser.Alert, deadline: Deadline, cache: _ContextCache
) -> dict[str, Any]:
    """Backstop so one alert's unexpected failure cannot lose the whole group.

    `_process_alert` handles the failures it knows about. Anything escaping it
    would propagate out of `future.result()` and fail the invocation, which is
    the retry-and-duplicate path this whole function exists to avoid.
    """
    try:
        return _process_alert(alert, deadline, cache)
    except Exception as exc:  # noqa: BLE001 - one alert must not sink the batch
        log_event(
            logging.ERROR,
            "alert_processing_failed",
            "Alert processing raised an unexpected exception",
            alert_name=alert.name,
            error=str(exc),
        )
        return {"alert": alert.name, "summarized": False, "delivered": False, "error": str(exc)}


def _process_alert(
    alert: alert_parser.Alert, deadline: Deadline, cache: _ContextCache | None = None
) -> dict[str, Any]:
    alert_dict = alert.to_dict()

    log_event(
        logging.INFO,
        "alert_processing_started",
        "Processing alert",
        alert_name=alert.name,
        severity=alert.severity,
        service=alert.service,
        dedupe_key=alert.dedupe_key,
    )

    # Checked before enrichment, not after: starting a generation that cannot
    # finish spends the model call and still loses the alert. The raw page is
    # worth more than a summary that arrives as a timeout.
    if not deadline.allows(_ALERT_BUDGET_SECONDS):
        log_event(
            logging.WARNING,
            "summarization_skipped",
            "Too little time left to summarize; delivering the raw alert",
            alert_name=alert.name,
            remaining_seconds=round(deadline.total_remaining_seconds(), 1),
        )
        return {
            "alert": alert.name,
            "summarized": False,
            "delivered": slack.post_fallback(alert_dict, _DEADLINE_SKIP_ERROR),
            "error": _DEADLINE_SKIP_ERROR,
        }

    context = _gather_context(alert, deadline, cache)

    try:
        summary = prompt.summarize(alert_dict, context)
    except Exception as exc:  # noqa: BLE001 - any failure here falls back to raw delivery
        log_event(
            logging.ERROR,
            "summarization_failed",
            "Falling back to unsummarized notification",
            alert_name=alert.name,
            error=str(exc),
        )
        delivered = slack.post_fallback(alert_dict, str(exc))
        return {
            "alert": alert.name,
            "summarized": False,
            "delivered": delivered,
            "error": str(exc),
        }

    delivered = slack.post_summary(alert_dict, summary, context)

    return {
        "alert": alert.name,
        "summarized": True,
        "delivered": delivered,
        "confidence": summary.get("confidence"),
        "assessed_severity": summary.get("severity_assessment"),
        "context_sources": sorted(
            k for k, v in context.items() if isinstance(v, dict) and v.get("available")
        ),
    }


class _ContextCache:
    """Memoizes collector results for the life of one invocation.

    Alerts in one Grafana group share `commonLabels`, so they routinely carry
    the same service and cluster. Without this, six collectors re-run per alert
    with identical arguments — the same `describe_alarms`, the same metric
    fetch, and the same Logs Insights query, which is both the slowest step in
    the pipeline and the one that bills per scan.

    Keyed on the arguments rather than the collector name alone, so alerts on
    different services still each get their own lookups.
    """

    def __init__(self) -> None:
        self._results: dict[tuple[Any, ...], dict[str, Any]] = {}
        self._locks: dict[tuple[Any, ...], threading.Lock] = {}
        self._guard = threading.Lock()

    def get(self, key: tuple[Any, ...], compute: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        with self._guard:
            if key in self._results:
                return self._results[key]
            lock = self._locks.setdefault(key, threading.Lock())

        # Held across the call so concurrent alerts wait for the first result
        # instead of racing to duplicate the work this class exists to avoid.
        with lock:
            with self._guard:
                if key in self._results:
                    return self._results[key]

            result = compute()

            with self._guard:
                self._results[key] = result
            return result


def _gather_context(
    alert: alert_parser.Alert, deadline: Deadline, cache: _ContextCache | None = None
) -> dict[str, Any]:
    """Run every collector concurrently.

    These are all I/O-bound API calls, so threads are the right tool — and
    concurrency is what makes five enrichment sources fit inside a 60s timeout.
    Each collector handles its own errors; the wrapper below is a backstop for
    anything that escapes, so one unexpected exception cannot lose the other
    four sources' results.

    `cache` is shared across the alerts of one group; omitted, each call gets a
    private one and behaves exactly as an uncached gather.
    """
    cache = _ContextCache() if cache is None else cache

    # (cache key, collector). The key names every argument the collector reads,
    # so two alerts share a result only when the call would have been identical.
    collectors: dict[str, tuple[tuple[Any, ...], Callable[[], dict[str, Any]]]] = {
        "ecs_service": (
            ("ecs_service", alert.service, alert.cluster),
            lambda: ecs.collect(alert.service, alert.cluster, deadline),
        ),
        "cloudwatch_alarms": (
            ("cloudwatch_alarms", alert.name, alert.service),
            lambda: cloudwatch.collect_alarms(alert.name, alert.service, deadline),
        ),
        "cloudwatch_metrics": (
            ("cloudwatch_metrics", alert.cluster, alert.service),
            lambda: cloudwatch.collect_metrics(alert.cluster, alert.service, deadline),
        ),
        "cloudwatch_logs": (
            ("cloudwatch_logs", alert.service),
            lambda: cloudwatch.collect_logs(alert.service, deadline),
        ),
        "grafana": (
            ("grafana", alert.name),
            lambda: grafana.collect(alert.name, deadline),
        ),
        "splunk": (
            ("splunk", alert.service),
            lambda: splunk.collect(alert.service, deadline),
        ),
    }

    started = time.monotonic()

    with ThreadPoolExecutor(max_workers=len(collectors)) as pool:
        # partial rather than a lambda: a lambda closing over the comprehension
        # variables would bind them late and hand every collector the last pair.
        futures = {
            name: pool.submit(cache.get, key, partial(_safe_collect, name, fn))
            for name, (key, fn) in collectors.items()
        }
        context = {name: future.result() for name, future in futures.items()}

    available = [name for name, payload in context.items() if payload.get("available")]

    log_event(
        logging.INFO,
        "context_gathered",
        "Collected incident context",
        alert_name=alert.name,
        sources_available=available,
        sources_unavailable=[n for n in context if n not in available],
        duration_ms=round((time.monotonic() - started) * 1000),
    )

    return context


def _safe_collect(name: str, collector: Any) -> dict[str, Any]:
    try:
        result = collector()
        return (
            result
            if isinstance(result, dict)
            else {"available": False, "reason": "Collector returned no data"}
        )
    except Exception as exc:  # noqa: BLE001 - a broken collector must not fail the invocation
        log_event(
            logging.WARNING,
            "context_collector_failed",
            "Context collector raised an unexpected exception",
            collector=name,
            error=str(exc),
        )
        return {"available": False, "reason": f"Collector error: {exc}"}


# ---------------------------------------------------------------------------
# Function URL authentication
# ---------------------------------------------------------------------------


def _is_function_url_request(event: dict[str, Any]) -> bool:
    return isinstance(event, dict) and "requestContext" in event and "headers" in event


def _reject_unauthenticated(event: dict[str, Any]) -> dict[str, Any] | None:
    """Verify the shared secret on a Function URL request.

    Grafana cannot sign SigV4, so the Function URL is configured with
    authorization NONE and authenticated here instead. Returns a response to
    send on rejection, or None when the request is authorized.
    """
    config = get_config()

    try:
        expected = get_secret(config.webhook_token_secret_id)
    except SecretNotConfigured:
        # Fail closed. An unauthenticated public endpoint that invokes a Bedrock
        # model is an availability and cost problem, not just a security one.
        log_event(
            logging.ERROR,
            "webhook_token_missing",
            "Rejecting Function URL request: shared secret is not configured",
        )
        return _response(503, {"error": "Endpoint not configured"})

    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    presented = headers.get("x-webhook-token", "")

    # Constant-time comparison: a plain == leaks the token one byte at a time to
    # anyone able to measure response latency.
    if not hmac.compare_digest(presented, expected):
        log_event(
            logging.WARNING,
            "webhook_auth_rejected",
            "Rejected Function URL request with missing or invalid token",
            source_ip=(event.get("requestContext", {}).get("http", {}) or {}).get("sourceIp"),
        )
        return _response(401, {"error": "Unauthorized"})

    return None


def _response(status: int, body: dict[str, Any]) -> dict[str, Any]:
    """Shape a response that satisfies both invocation paths.

    Function URL callers need statusCode and a serialized body; EventBridge
    ignores the return value entirely, so the extra fields are harmless.
    """
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str),
    }
