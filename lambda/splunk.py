"""Splunk enrichment.

CloudWatch Logs covers what AWS emits; Splunk usually holds everything else —
application logs, upstream services, and the log lines from before the alert
window that show where a fault actually started.

Uses the one-shot search endpoint (`exec_mode=oneshot`) rather than creating a
search job and polling it. A summarizer that cannot answer within its timeout is
not useful, and one-shot keeps the whole interaction to a single request.
"""

from __future__ import annotations

import logging
from typing import Any

from config import Deadline, SecretNotConfigured, get_config, get_secret, log_event
from http_client import HttpError, request

_SEARCH_PATH = "/services/search/jobs/export"
_MAX_RESULTS = 15

# Grouping by log level and message keeps the response small enough to be worth
# putting in a prompt; the raw events would be mostly duplicates.
_SEARCH_TEMPLATE = (
    "search index={index} {scope} "
    "(error OR exception OR fatal OR critical OR timeout OR refused) "
    "earliest=-{minutes}m latest=now "
    "| stats count as occurrences, latest(_time) as last_seen by source, log_level, punct "
    "| sort - occurrences | head {limit}"
)


def collect(service: str | None, deadline: Deadline) -> dict[str, Any]:
    """Search Splunk for error events correlated with the alert window."""
    config = get_config()

    if not config.splunk_enabled:
        return {"available": False, "reason": "Splunk integration not configured"}
    if not service:
        return {"available": False, "reason": "No service label to scope the search to"}

    # A Splunk search is the most expensive enrichment call. Skip it rather than
    # risk the whole invocation timing out before Bedrock is reached.
    if deadline.remaining_seconds() < 10:
        return {"available": False, "reason": "Skipped: insufficient time before invocation deadline"}

    try:
        token = get_secret(config.splunk_token_secret_id)
    except SecretNotConfigured as exc:
        log_event(logging.WARNING, "splunk_not_configured", str(exc))
        return {"available": False, "reason": "Splunk token not populated"}

    query = _SEARCH_TEMPLATE.format(
        index=_sanitize(config.splunk_index),
        scope=_sanitize(service),
        minutes=config.lookback_minutes,
        limit=_MAX_RESULTS,
    )

    try:
        response = request(
            "POST",
            f"{config.splunk_base_url}{_SEARCH_PATH}",
            headers={"Authorization": f"Bearer {token}"},
            form={
                "search": query,
                "output_mode": "json",
                "exec_mode": "oneshot",
                # Server-side cap. Without it a broad search can stream for the
                # rest of the invocation budget.
                "max_count": str(_MAX_RESULTS),
                "timeout": str(int(min(20, deadline.remaining_seconds()))),
            },
            timeout=min(20.0, deadline.remaining_seconds()),
        )
    except HttpError as exc:
        log_event(logging.WARNING, "splunk_search_failed", "Splunk search failed", error=str(exc))
        return {"available": False, "reason": f"Splunk unreachable: {exc}"}

    if not response.ok:
        log_event(
            logging.WARNING,
            "splunk_search_failed",
            "Splunk returned a non-success status",
            status=response.status,
        )
        return {"available": False, "reason": f"Splunk returned HTTP {response.status}"}

    events = _parse_export(response.body)

    return {
        "available": True,
        "index": config.splunk_index,
        "window_minutes": config.lookback_minutes,
        "query": query,
        "distinct_patterns": len(events),
        "total_occurrences": sum(e.get("occurrences", 0) for e in events),
        "top_errors": events,
    }


def _sanitize(value: str) -> str:
    """Strip SPL metacharacters from values interpolated into a search.

    Alert labels originate outside this system: a service label containing a
    pipe would otherwise append arbitrary commands to the search.
    """
    return "".join(c for c in value if c.isalnum() or c in "-_.:/*")


def _parse_export(body: str) -> list[dict[str, Any]]:
    """Parse the export endpoint's newline-delimited JSON.

    Each line is a separate JSON document, and the stream interleaves result
    rows with progress messages — so a whole-body json.loads would fail.
    """
    import json

    rows: list[dict[str, Any]] = []
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            document = json.loads(line)
        except json.JSONDecodeError:
            continue

        result = document.get("result")
        if not isinstance(result, dict):
            continue

        rows.append(
            {
                "source": result.get("source"),
                "log_level": result.get("log_level"),
                "pattern": (result.get("punct") or "")[:200],
                "occurrences": int(result.get("occurrences", 0) or 0),
                "last_seen": result.get("last_seen"),
            }
        )

    return rows[:_MAX_RESULTS]
