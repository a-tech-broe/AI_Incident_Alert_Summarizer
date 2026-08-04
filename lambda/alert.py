"""Normalization of inbound alert payloads.

Two ingestion paths reach this Lambda and they carry different envelopes:

  * EventBridge  — the alert sits under `detail`, wrapped in AWS metadata.
  * Function URL — an API Gateway v2 style HTTP request with a JSON body.

Grafana's unified alerting webhook groups multiple firing instances into a
single POST. Each instance becomes its own `Alert` so the summarizer reasons
about one problem at a time rather than a blended group.
"""

from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from config import log_event

# Labels Grafana alert rules conventionally carry. Used to locate the affected
# workload without hard-coding one organization's labelling scheme.
_SERVICE_LABELS = ("service", "service_name", "app", "application", "ecs_service", "job")
_CLUSTER_LABELS = ("cluster", "ecs_cluster", "cluster_name", "kubernetes_cluster")
_SEVERITY_LABELS = ("severity", "priority", "level")


class UnparseableAlert(ValueError):
    """The payload did not contain anything recognizable as an alert."""


@dataclass
class Alert:
    """One alert instance, normalized across ingestion paths."""

    name: str
    status: str = "firing"
    severity: str = "unknown"
    summary: str = ""
    description: str = ""
    service: str | None = None
    cluster: str | None = None
    starts_at: str | None = None
    fingerprint: str | None = None
    runbook_url: str | None = None
    dashboard_url: str | None = None
    generator_url: str | None = None
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    values: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_firing(self) -> bool:
        return self.status.lower() in ("firing", "alerting", "active")

    @property
    def dedupe_key(self) -> str:
        """Stable identifier for correlating repeat firings of the same alert."""
        return self.fingerprint or f"{self.name}:{self.service or 'unknown'}"

    def age_minutes(self) -> float | None:
        if not self.starts_at:
            return None
        try:
            started = datetime.fromisoformat(self.starts_at.replace("Z", "+00:00"))
        except ValueError:
            return None
        return (datetime.now(UTC) - started).total_seconds() / 60.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "severity": self.severity,
            "summary": self.summary,
            "description": self.description,
            "service": self.service,
            "cluster": self.cluster,
            "starts_at": self.starts_at,
            "age_minutes": self.age_minutes(),
            "runbook_url": self.runbook_url,
            "dashboard_url": self.dashboard_url,
            "labels": self.labels,
            "annotations": self.annotations,
            "values": self.values,
        }


def _first_label(source: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = source.get(key)
        if value:
            return str(value)
    return None


def unwrap_payload(event: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Strip the transport envelope and return (payload, source_path)."""
    if not isinstance(event, dict):
        raise UnparseableAlert(f"Expected a dict event, got {type(event).__name__}")

    # Function URL / API Gateway HTTP API
    if "requestContext" in event and "body" in event:
        body = event.get("body") or "{}"
        if event.get("isBase64Encoded"):
            body = base64.b64decode(body).decode("utf-8")
        try:
            return json.loads(body), "function_url"
        except json.JSONDecodeError as exc:
            raise UnparseableAlert(f"Function URL body was not JSON: {exc}") from exc

    # EventBridge
    if "detail" in event and "detail-type" in event:
        detail = event["detail"]
        if isinstance(detail, str):
            try:
                detail = json.loads(detail)
            except json.JSONDecodeError as exc:
                raise UnparseableAlert(f"EventBridge detail was not JSON: {exc}") from exc
        if not isinstance(detail, dict):
            raise UnparseableAlert("EventBridge detail was not an object")
        return detail, "eventbridge"

    # Direct invocation — the console test button, or a manual replay.
    return event, "direct"


def parse(event: dict[str, Any]) -> tuple[list[Alert], str]:
    """Normalize an inbound event into individual alerts.

    Returns the alerts alongside the ingestion path they arrived on, which is
    logged so a misrouted producer is obvious from the logs alone.
    """
    payload, source_path = unwrap_payload(event)

    # Grafana unified alerting: a group of instances under `alerts`.
    instances = payload.get("alerts")
    if isinstance(instances, list) and instances:
        alerts = [_from_grafana_instance(i, payload) for i in instances if isinstance(i, dict)]
        truncated = payload.get("truncatedAlerts") or 0
        if truncated:
            # Grafana caps group size; without this the count in the summary
            # would understate the blast radius.
            log_event(
                logging.WARNING,
                "alert_group_truncated",
                "Grafana truncated the alert group; some instances were dropped upstream",
                truncated_count=truncated,
                received_count=len(alerts),
            )
        if alerts:
            return alerts, source_path

    # A single alert object, or a legacy Grafana payload.
    single = _from_flat_payload(payload)
    if single is not None:
        return [single], source_path

    raise UnparseableAlert("Payload contained no recognizable alert fields")


def _from_grafana_instance(instance: dict[str, Any], group: dict[str, Any]) -> Alert:
    # Group-level labels apply to every instance; instance labels win on conflict.
    labels = {**(group.get("commonLabels") or {}), **(instance.get("labels") or {})}
    annotations = {
        **(group.get("commonAnnotations") or {}),
        **(instance.get("annotations") or {}),
    }

    return Alert(
        name=labels.get("alertname") or group.get("title") or "Unnamed alert",
        status=instance.get("status") or group.get("status") or "firing",
        severity=_first_label(labels, _SEVERITY_LABELS) or "unknown",
        summary=annotations.get("summary", ""),
        description=annotations.get("description", "") or group.get("message", ""),
        service=_first_label(labels, _SERVICE_LABELS),
        cluster=_first_label(labels, _CLUSTER_LABELS),
        starts_at=instance.get("startsAt"),
        fingerprint=instance.get("fingerprint"),
        runbook_url=annotations.get("runbook_url") or annotations.get("runbook"),
        dashboard_url=instance.get("dashboardURL") or group.get("externalURL"),
        generator_url=instance.get("generatorURL"),
        labels={str(k): str(v) for k, v in labels.items()},
        annotations={str(k): str(v) for k, v in annotations.items()},
        values=instance.get("values") or {},
        raw=instance,
    )


def _from_flat_payload(payload: dict[str, Any]) -> Alert | None:
    labels = payload.get("labels") or payload.get("tags") or {}
    annotations = payload.get("annotations") or {}

    name = (
        labels.get("alertname")
        or payload.get("alertname")
        or payload.get("ruleName")
        or payload.get("title")
        or payload.get("alarmName")  # CloudWatch alarm via EventBridge
        or payload.get("AlarmName")
    )
    if not name:
        return None

    return Alert(
        name=str(name),
        status=str(payload.get("status") or payload.get("state") or "firing"),
        severity=_first_label({**labels, **payload}, _SEVERITY_LABELS) or "unknown",
        summary=annotations.get("summary", "") or payload.get("message", ""),
        description=annotations.get("description", "") or payload.get("description", ""),
        service=_first_label({**labels, **payload}, _SERVICE_LABELS),
        cluster=_first_label({**labels, **payload}, _CLUSTER_LABELS),
        starts_at=payload.get("startsAt") or payload.get("time"),
        fingerprint=payload.get("fingerprint"),
        runbook_url=annotations.get("runbook_url"),
        dashboard_url=payload.get("dashboardURL"),
        generator_url=payload.get("generatorURL") or payload.get("ruleUrl"),
        labels={str(k): str(v) for k, v in labels.items()},
        annotations={str(k): str(v) for k, v in annotations.items()},
        values=payload.get("values") or payload.get("evalMatches") or {},
        raw=payload,
    )
