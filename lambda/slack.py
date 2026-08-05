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

# Maps the failure to something a responder can act on. Ordered — first match
# wins, so put the more specific patterns first.
_FAILURE_REASONS: tuple[tuple[str, str], ...] = (
    ("Too many tokens", "Bedrock daily token quota is exhausted. It resets on a rolling 24h basis."),
    ("ThrottlingException", "Bedrock is throttling requests. Retry shortly."),
    ("AccessDeniedException", "Bedrock model access is not granted for this account and region."),
    ("ValidationException", "Bedrock rejected the request — check the configured model ID."),
    ("ModelTimeoutException", "Bedrock timed out generating the summary."),
    ("ReadTimeoutError", "Bedrock timed out generating the summary."),
    ("ServiceUnavailable", "Bedrock is temporarily unavailable."),
    ("still holds the Terraform placeholder", "A required credential has not been configured."),
    ("no JSON object", "The model returned output the parser could not read."),
)


def _explain_failure(error: str) -> str:
    """Turn an exception string into one actionable sentence."""
    for needle, reason in _FAILURE_REASONS:
        if needle in error:
            return reason
    return "Summarization failed. See CloudWatch Logs for the full error."


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
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"⚠️ {_truncate(alert.get('name', 'Alert'), 140)}"},
        },
    ]

    # The alert's own text is what the responder needs first — it is the only
    # description of the problem when there is no summary. Put it directly under
    # the headline, above the machinery.
    description = alert.get("summary") or alert.get("description")
    if description:
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(description, _MAX_TEXT)}}
        )

    blocks.append(
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity*\n{alert.get('severity', 'unknown')}"},
                {"type": "mrkdwn", "text": f"*Service*\n{alert.get('service') or 'unknown'}"},
            ],
        }
    )

    # Why summarization failed, in a sentence — not the raw exception.
    #
    # A boto traceback tells an on-call engineer nothing actionable, buries the
    # alert it is attached to, and is an uncontrolled string being posted into a
    # chat channel. The full error is in CloudWatch Logs, where it belongs.
    blocks.append(
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"_No AI summary: {_explain_failure(error)}_"}],
        }
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
