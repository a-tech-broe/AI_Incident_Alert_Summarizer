"""Grafana enrichment.

The webhook payload says an alert fired; it does not say what the rule actually
measures, what else is firing, or whether someone already silenced it. Those
come from the Grafana API and materially change the recommended action — there
is no point paging on something an engineer silenced ten minutes ago.
"""

from __future__ import annotations

import logging
from typing import Any

from config import Deadline, SecretNotConfigured, get_config, get_secret, log_event
from http_client import HttpError, request

_TIMEOUT = 6.0


def collect(alert_name: str, deadline: Deadline) -> dict[str, Any]:
    """Fetch rule definition, sibling firing alerts and active silences."""
    config = get_config()

    if not config.grafana_enabled:
        return {"available": False, "reason": "Grafana integration not configured"}
    if deadline.expired():
        return {"available": False, "reason": "Skipped: invocation deadline reached"}

    try:
        token = get_secret(config.grafana_token_secret_id)
    except SecretNotConfigured as exc:
        log_event(logging.WARNING, "grafana_not_configured", str(exc))
        return {"available": False, "reason": "Grafana token not populated"}

    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    base = config.grafana_base_url

    context: dict[str, Any] = {"available": True, "base_url": base}

    rule = _fetch_rule(base, headers, alert_name)
    if rule:
        context["rule"] = rule

    if not deadline.expired():
        firing = _fetch_firing_alerts(base, headers)
        if firing is not None:
            context["other_firing_alerts"] = firing
            context["total_firing"] = len(firing)

    if not deadline.expired():
        silences = _fetch_silences(base, headers, alert_name)
        if silences is not None:
            context["active_silences"] = silences
            if silences:
                context["silence_note"] = (
                    "This alert is covered by an active silence — it may be a known "
                    "or already-acknowledged condition."
                )

    return context


def _get(base: str, path: str, headers: dict[str, str]) -> Any | None:
    try:
        response = request("GET", f"{base}{path}", headers=headers, timeout=_TIMEOUT)
    except HttpError as exc:
        log_event(
            logging.WARNING, "grafana_request_failed", "Grafana request failed", path=path, error=str(exc)
        )
        return None

    if not response.ok:
        log_event(
            logging.WARNING,
            "grafana_request_failed",
            "Grafana returned a non-success status",
            path=path,
            status=response.status,
        )
        return None

    return response.json()


def _fetch_rule(base: str, headers: dict[str, str], alert_name: str) -> dict[str, Any] | None:
    """Locate the provisioned rule behind this alert.

    The rule's query and evaluation window explain what the alert actually
    asserts, which the webhook payload alone never conveys.
    """
    rules = _get(base, "/api/v1/provisioning/alert-rules", headers)
    if not isinstance(rules, list):
        return None

    match = next((r for r in rules if r.get("title") == alert_name), None)
    if match is None:
        match = next((r for r in rules if alert_name.lower() in str(r.get("title", "")).lower()), None)
    if match is None:
        return None

    return {
        "title": match.get("title"),
        "folder": match.get("folderUID"),
        "condition": match.get("condition"),
        "for_duration": match.get("for"),
        "no_data_state": match.get("noDataState"),
        "exec_err_state": match.get("execErrState"),
        "annotations": match.get("annotations", {}),
        "labels": match.get("labels", {}),
        "queries": _summarize_queries(match.get("data", [])),
    }


def _summarize_queries(data: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the expression and time range; drop the datasource plumbing that
    would otherwise dominate the prompt."""
    summarized = []
    for item in data:
        model = item.get("model", {}) or {}
        summarized.append(
            {
                "ref_id": item.get("refId"),
                "expression": model.get("expr") or model.get("expression") or model.get("query"),
                "relative_time_range": item.get("relativeTimeRange"),
                "reducer": model.get("reducer"),
            }
        )
    return summarized


def _fetch_firing_alerts(base: str, headers: dict[str, str]) -> list[dict[str, Any]] | None:
    """Other alerts firing right now — the clearest available blast-radius signal."""
    alerts = _get(base, "/api/alertmanager/grafana/api/v2/alerts?active=true&silenced=false", headers)
    if not isinstance(alerts, list):
        return None

    firing = []
    for alert in alerts[:25]:
        labels = alert.get("labels", {}) or {}
        firing.append(
            {
                "name": labels.get("alertname"),
                "severity": labels.get("severity"),
                "service": labels.get("service") or labels.get("job"),
                "started_at": alert.get("startsAt"),
            }
        )
    return firing


def _fetch_silences(base: str, headers: dict[str, str], alert_name: str) -> list[dict[str, Any]] | None:
    silences = _get(base, "/api/alertmanager/grafana/api/v2/silences", headers)
    if not isinstance(silences, list):
        return None

    active = []
    for silence in silences:
        if (silence.get("status", {}) or {}).get("state") != "active":
            continue

        matchers = silence.get("matchers", []) or []
        relevant = any(
            m.get("name") == "alertname" and alert_name.lower() in str(m.get("value", "")).lower()
            for m in matchers
        )
        if not relevant:
            continue

        active.append(
            {
                "comment": silence.get("comment"),
                "created_by": silence.get("createdBy"),
                "ends_at": silence.get("endsAt"),
            }
        )
    return active
