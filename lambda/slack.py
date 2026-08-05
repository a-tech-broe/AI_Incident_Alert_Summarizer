"""Slack delivery.

The message is built with Block Kit rather than a markdown blob so the parts a
responder scans first — headline, severity, confidence — hold fixed positions.
At 3am, layout stability is worth more than density.

The AI-generated section is visually separated and carries the model's own
confidence rating. Presenting an inference with the same weight as a metric
reading is how an automated summary starts costing more time than it saves.
"""

from __future__ import annotations

import logging
from typing import Any

from config import SecretNotConfigured, get_config, get_secret, log_event
from http_client import HttpError, request

_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
    "unknown": "⚪",
}

_CONFIDENCE_NOTE = {
    "high": "High confidence — evidence directly supports this analysis.",
    "medium": "Medium confidence — analysis is plausible but not conclusive. Verify before acting.",
    "low": "Low confidence — insufficient telemetry. Treat as a starting point only.",
}

# Slack truncates blocks past ~3000 characters and rejects messages over 50
# blocks, so lists are capped rather than left unbounded.
_MAX_LIST_ITEMS = 6
_MAX_TEXT = 2800


def post_summary(alert: dict[str, Any], summary: dict[str, Any], context: dict[str, Any]) -> bool:
    """Post a summarized incident to Slack. Returns delivery success."""
    blocks = _build_blocks(alert, summary, context)
    fallback = f"{alert.get('name', 'Alert')}: {summary.get('headline', '')}"
    return _post({"text": fallback[:200], "blocks": blocks})


def post_fallback(alert: dict[str, Any], error: str) -> bool:
    """Post the raw alert when summarization failed.

    The pipeline degrading must never mean silence: an unsummarized page is far
    better than a page that never arrives.
    """
    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"⚠️ {_truncate(alert.get('name', 'Alert'), 140)}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (f"*AI summarization unavailable — raw alert follows.*\n`{_truncate(error, 300)}`"),
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity*\n{alert.get('severity', 'unknown')}"},
                {"type": "mrkdwn", "text": f"*Service*\n{alert.get('service') or 'unknown'}"},
            ],
        },
    ]

    description = alert.get("summary") or alert.get("description")
    if description:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(description, _MAX_TEXT)}}
        )

    links = _link_block(alert)
    if links:
        blocks.append(links)

    return _post({"text": f"Alert (unsummarized): {alert.get('name', 'Alert')}", "blocks": blocks})


# ---------------------------------------------------------------------------
# Message construction
# ---------------------------------------------------------------------------


def _build_blocks(
    alert: dict[str, Any], summary: dict[str, Any], context: dict[str, Any]
) -> list[dict[str, Any]]:
    config = get_config()
    severity = summary.get("severity_assessment", "unknown")
    emoji = _SEVERITY_EMOJI.get(severity, "⚪")

    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": _truncate(f"{emoji} {summary['headline']}", 150)},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Alert*\n{_truncate(alert.get('name', 'unknown'), 100)}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Severity*\n{severity} _(alert said: {alert.get('severity', 'unknown')})_",
                },
                {"type": "mrkdwn", "text": f"*Service*\n{alert.get('service') or '_not labelled_'}"},
                {"type": "mrkdwn", "text": f"*Environment*\n{config.environment}"},
            ],
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Impact*\n{_truncate(summary['impact'], _MAX_TEXT)}"},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Probable cause*\n{_truncate(summary['probable_cause'], _MAX_TEXT)}",
            },
        },
    ]

    # Confidence sits immediately under the cause, where it is read as a
    # qualifier on that claim rather than as a footnote.
    confidence = summary.get("confidence", "low")
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"_{_CONFIDENCE_NOTE.get(confidence, '')}_"}],
        }
    )

    if summary.get("evidence"):
        blocks.append(_bullet_section("Evidence", summary["evidence"]))

    if summary.get("recommended_actions"):
        blocks.append(_numbered_section("Recommended actions", summary["recommended_actions"]))

    if summary.get("context_gaps"):
        blocks.append(_bullet_section("Gaps in available telemetry", summary["context_gaps"]))

    health = _service_health_line(context)
    if health:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Service state*\n{health}"}})

    links = _link_block(alert)
    if links:
        blocks.append(links)

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Generated by {config.project_name} · {config.model_id} · "
                        f"sources: {_sources_line(context)}"
                    ),
                }
            ],
        }
    )

    return blocks


def _bullet_section(title: str, items: list[str]) -> dict[str, Any]:
    lines = "\n".join(f"• {_truncate(item, 300)}" for item in items[:_MAX_LIST_ITEMS])
    if len(items) > _MAX_LIST_ITEMS:
        lines += f"\n_…and {len(items) - _MAX_LIST_ITEMS} more_"
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n{lines}"}}


def _numbered_section(title: str, items: list[str]) -> dict[str, Any]:
    lines = "\n".join(
        f"{i}. {_truncate(item, 300)}" for i, item in enumerate(items[:_MAX_LIST_ITEMS], start=1)
    )
    if len(items) > _MAX_LIST_ITEMS:
        lines += f"\n_…and {len(items) - _MAX_LIST_ITEMS} more_"
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"*{title}*\n{lines}"}}


def _service_health_line(context: dict[str, Any]) -> str | None:
    ecs = context.get("ecs_service", {})
    if isinstance(ecs, dict) and ecs.get("available"):
        return ecs.get("health_summary")
    return None


def _link_block(alert: dict[str, Any]) -> dict[str, Any] | None:
    links = []
    for label, key in (
        ("Runbook", "runbook_url"),
        ("Dashboard", "dashboard_url"),
        ("Alert rule", "generator_url"),
    ):
        url = alert.get(key)
        if url:
            links.append(f"<{url}|{label}>")

    if not links:
        return None

    return {"type": "context", "elements": [{"type": "mrkdwn", "text": " · ".join(links)}]}


def _sources_line(context: dict[str, Any]) -> str:
    available = [
        name.replace("_", " ")
        for name, payload in context.items()
        if isinstance(payload, dict) and payload.get("available")
    ]
    return ", ".join(available) if available else "alert payload only"


def _truncate(text: Any, limit: int) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Delivery
# ---------------------------------------------------------------------------


def _post(payload: dict[str, Any]) -> bool:
    config = get_config()

    try:
        webhook_url = get_secret(config.slack_webhook_secret_id)
    except SecretNotConfigured as exc:
        log_event(logging.ERROR, "slack_post_failed", "Slack webhook not configured", error=str(exc))
        return False

    try:
        response = request("POST", webhook_url, body=payload, timeout=10.0, delivery=True)
    except HttpError as exc:
        log_event(logging.ERROR, "slack_post_failed", "Slack webhook unreachable", error=str(exc))
        return False

    if not response.ok:
        # Slack returns the reason as a plain-text body (invalid_blocks,
        # channel_not_found, …) — worth logging verbatim, it names the fix.
        log_event(
            logging.ERROR,
            "slack_post_failed",
            "Slack rejected the message",
            status=response.status,
            slack_response=response.body[:200],
        )
        return False

    log_event(logging.INFO, "slack_post_succeeded", "Posted incident summary to Slack")
    return True
