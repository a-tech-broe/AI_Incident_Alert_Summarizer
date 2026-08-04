"""ECS context collection.

Answers the first question a responder asks about a service alert: what changed,
and is the service actually healthy right now? Deployment state and stopped-task
reasons account for most of what a human would check in the console.
"""

from __future__ import annotations

import logging
from typing import Any

from botocore.exceptions import ClientError

from config import Deadline, get_client, get_config, log_event

# Enough to spot a pattern in stop reasons without dragging the whole task list
# into the prompt.
_MAX_STOPPED_TASKS = 10
_MAX_EVENTS = 8


def collect(service_hint: str | None, cluster_hint: str | None, deadline: Deadline) -> dict[str, Any]:
    """Gather ECS state for the service an alert points at.

    Returns a dict that is always safe to serialize into the prompt; failures are
    reported in-band as an `error` key rather than raised, so one degraded
    dependency cannot suppress the whole summary.
    """
    if not service_hint:
        return {"available": False, "reason": "Alert carried no service label"}

    if deadline.expired():
        return {"available": False, "reason": "Skipped: invocation deadline reached"}

    config = get_config()
    client = get_client("ecs")

    try:
        cluster = _resolve_cluster(client, cluster_hint, config.ecs_cluster_allowlist)
        if cluster is None:
            return {"available": False, "reason": "No ECS cluster could be resolved"}

        service = _describe_service(client, cluster, service_hint)
        if service is None:
            return {
                "available": False,
                "reason": f"Service '{service_hint}' not found in cluster '{cluster}'",
            }

        context: dict[str, Any] = {
            "available": True,
            "cluster": cluster,
            "service": service["serviceName"],
            "status": service.get("status"),
            "desired_count": service.get("desiredCount"),
            "running_count": service.get("runningCount"),
            "pending_count": service.get("pendingCount"),
            "task_definition": service.get("taskDefinition", "").split("/")[-1],
            "deployments": _summarize_deployments(service.get("deployments", [])),
            "recent_events": _summarize_events(service.get("events", [])),
        }

        # A service sitting at desired != running is the whole story; the stopped
        # task reasons tell you why. Only pay for that call when it matters.
        if service.get("runningCount", 0) < service.get("desiredCount", 0) and not deadline.expired():
            context["stopped_tasks"] = _describe_stopped_tasks(client, cluster, service["serviceName"])

        context["health_summary"] = _health_summary(context)
        return context

    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "Unknown")
        log_event(
            logging.WARNING,
            "ecs_collect_failed",
            "ECS context collection failed",
            error_code=code,
            service=service_hint,
            cluster=cluster_hint,
        )
        return {"available": False, "reason": f"ECS API error: {code}"}
    except Exception as exc:  # noqa: BLE001 - enrichment must never break the summary
        log_event(
            logging.WARNING,
            "ecs_collect_failed",
            "Unexpected error collecting ECS context",
            error=str(exc),
            service=service_hint,
        )
        return {"available": False, "reason": f"Unexpected error: {exc}"}


def _resolve_cluster(client: Any, hint: str | None, allowlist: tuple[str, ...]) -> str | None:
    """Pick the cluster to query, preferring the alert's own label."""
    if hint:
        # Alerts sometimes carry the full ARN rather than the bare name.
        return hint.split("/")[-1]

    if allowlist:
        return allowlist[0]

    clusters = client.list_clusters().get("clusterArns", [])
    if len(clusters) == 1:
        return clusters[0].split("/")[-1]

    # Guessing among several clusters would produce confidently wrong context,
    # which is worse than no context.
    return None


def _describe_service(client: Any, cluster: str, service_hint: str) -> dict[str, Any] | None:
    response = client.describe_services(cluster=cluster, services=[service_hint])
    services = response.get("services", [])
    if services and services[0].get("status") != "INACTIVE":
        return services[0]

    # The alert label may be a prefix or a friendly name rather than the exact
    # ECS service name; fall back to a substring match before giving up.
    paginator = client.get_paginator("list_services")
    for page in paginator.paginate(cluster=cluster):
        for arn in page.get("serviceArns", []):
            name = arn.split("/")[-1]
            if service_hint.lower() in name.lower():
                match = client.describe_services(cluster=cluster, services=[name]).get("services", [])
                if match:
                    return match[0]
    return None


def _summarize_deployments(deployments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Condense deployment records. A PRIMARY that is not COMPLETED means a
    rollout is in flight, which is the most common cause of a sudden alert."""
    summary = []
    for deployment in deployments:
        summary.append(
            {
                "status": deployment.get("status"),
                "rollout_state": deployment.get("rolloutState"),
                "rollout_reason": deployment.get("rolloutStateReason"),
                "task_definition": deployment.get("taskDefinition", "").split("/")[-1],
                "desired": deployment.get("desiredCount"),
                "running": deployment.get("runningCount"),
                "failed": deployment.get("failedTasks"),
                "created_at": deployment.get("createdAt"),
                "updated_at": deployment.get("updatedAt"),
            }
        )
    return summary


def _summarize_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"at": event.get("createdAt"), "message": event.get("message")} for event in events[:_MAX_EVENTS]]


def _describe_stopped_tasks(client: Any, cluster: str, service: str) -> list[dict[str, Any]]:
    task_arns = client.list_tasks(
        cluster=cluster,
        serviceName=service,
        desiredStatus="STOPPED",
        maxResults=_MAX_STOPPED_TASKS,
    ).get("taskArns", [])

    if not task_arns:
        return []

    tasks = client.describe_tasks(cluster=cluster, tasks=task_arns).get("tasks", [])

    stopped = []
    for task in tasks:
        containers = [
            {
                "name": container.get("name"),
                "exit_code": container.get("exitCode"),
                "reason": container.get("reason"),
            }
            for container in task.get("containers", [])
            # An exit code of 0 with no reason is a normal scale-down, not a
            # failure worth showing the responder.
            if container.get("exitCode") not in (0, None) or container.get("reason")
        ]

        stopped.append(
            {
                "stopped_at": task.get("stoppedAt"),
                "stop_code": task.get("stopCode"),
                "stopped_reason": task.get("stoppedReason"),
                "containers": containers,
            }
        )
    return stopped


def _health_summary(context: dict[str, Any]) -> str:
    """One-line verdict, so the model does not have to infer it from counts."""
    desired = context.get("desired_count") or 0
    running = context.get("running_count") or 0

    primary = next(
        (d for d in context.get("deployments", []) if d.get("status") == "PRIMARY"),
        None,
    )
    rollout_in_progress = bool(primary and primary.get("rollout_state") == "IN_PROGRESS")

    if rollout_in_progress:
        return f"Deployment in progress ({running}/{desired} tasks running)"
    if running == 0 and desired > 0:
        return f"Service fully down: 0 of {desired} desired tasks running"
    if running < desired:
        return f"Service degraded: {running} of {desired} desired tasks running"
    return f"Service at desired capacity ({running}/{desired})"
