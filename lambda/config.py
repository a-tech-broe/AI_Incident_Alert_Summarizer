"""Environment-derived configuration and shared runtime helpers.

Everything here is resolved once per container and reused across invocations.
Cold-start cost matters: an alert that takes 40s to summarize is an alert the
responder has already started debugging by hand.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, field
from functools import cache
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

# ---------------------------------------------------------------------------
# Structured logging
#
# The CloudWatch metric filters in terraform/modules/cloudwatch match on
# `$.level` and `$.event`, so every record must be single-line JSON carrying
# both fields. A bare `print` or an unstructured logger breaks those alarms
# silently.
# ---------------------------------------------------------------------------


class JsonFormatter(logging.Formatter):
    """Renders log records as one-line JSON."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "level": record.levelname,
            "event": getattr(record, "event", record.funcName),
            "message": record.getMessage(),
            "logger": record.name,
        }

        extras = getattr(record, "extra_fields", None)
        if extras:
            payload.update(extras)

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        # default=str keeps datetimes and boto response objects from raising
        # inside the logger, which would mask the error being reported.
        return json.dumps(payload, default=str)


def _build_logger() -> logging.Logger:
    logger = logging.getLogger("summarizer")
    logger.setLevel(os.environ.get("LOG_LEVEL", "INFO").upper())
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.handlers = [handler]
    return logger


LOG = _build_logger()


def log_event(level: int, event: str, message: str, **fields: Any) -> None:
    """Emit a structured record. `event` is what alarms and queries key on."""
    LOG.log(level, message, extra={"event": event, "extra_fields": fields})


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, "")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, "")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


@dataclass(frozen=True)
class Config:
    environment: str = os.environ.get("ENVIRONMENT", "dev")
    project_name: str = os.environ.get("PROJECT_NAME", "ai-incident-summarizer")
    region: str = os.environ.get("AWS_REGION", "us-east-1")

    # Bedrock
    model_id: str = os.environ.get("BEDROCK_MODEL_ID", "")
    max_tokens: int = field(default_factory=lambda: _int_env("BEDROCK_MAX_TOKENS", 2000))
    temperature: float = field(default_factory=lambda: _float_env("BEDROCK_TEMPERATURE", 0.2))
    guardrail_id: str = os.environ.get("BEDROCK_GUARDRAIL_ID", "")
    guardrail_version: str = os.environ.get("BEDROCK_GUARDRAIL_VER", "")

    # Integrations
    webhook_token_secret_id: str = os.environ.get("WEBHOOK_TOKEN_SECRET_ID", "")
    slack_webhook_secret_id: str = os.environ.get("SLACK_WEBHOOK_SECRET_ID", "")
    slack_channel: str = os.environ.get("SLACK_CHANNEL", "#incidents")
    grafana_base_url: str = os.environ.get("GRAFANA_BASE_URL", "").rstrip("/")
    grafana_token_secret_id: str = os.environ.get("GRAFANA_TOKEN_SECRET_ID", "")
    splunk_base_url: str = os.environ.get("SPLUNK_BASE_URL", "").rstrip("/")
    splunk_token_secret_id: str = os.environ.get("SPLUNK_TOKEN_SECRET_ID", "")
    splunk_index: str = os.environ.get("SPLUNK_INDEX", "main")

    # Context gathering
    lookback_minutes: int = field(default_factory=lambda: _int_env("CONTEXT_LOOKBACK_MINUTES", 30))
    ecs_cluster_allowlist: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            c.strip() for c in os.environ.get("ECS_CLUSTER_ALLOWLIST", "").split(",") if c.strip()
        )
    )

    @property
    def grafana_enabled(self) -> bool:
        return bool(self.grafana_base_url and self.grafana_token_secret_id)

    @property
    def splunk_enabled(self) -> bool:
        return bool(self.splunk_base_url and self.splunk_token_secret_id)

    @property
    def guardrail_enabled(self) -> bool:
        return bool(self.guardrail_id and self.guardrail_version)


@cache
def get_config() -> Config:
    return Config()


# ---------------------------------------------------------------------------
# AWS clients
#
# Built once per container. Read-only enrichment calls get a short timeout and
# few retries: it is better to summarize with partial context than to burn the
# invocation budget waiting on a degraded dependency. Bedrock gets a longer
# budget because generation is the one call that cannot be skipped.
# ---------------------------------------------------------------------------

_ENRICHMENT_CONFIG = BotoConfig(
    connect_timeout=3,
    read_timeout=8,
    retries={"max_attempts": 2, "mode": "standard"},
)

_BEDROCK_CONFIG = BotoConfig(
    connect_timeout=5,
    read_timeout=40,
    retries={"max_attempts": 3, "mode": "adaptive"},
)


@cache
def get_client(service: str) -> Any:
    config = _BEDROCK_CONFIG if service.startswith("bedrock") else _ENRICHMENT_CONFIG
    return boto3.client(service, region_name=get_config().region, config=config)


# ---------------------------------------------------------------------------
# Secrets
# ---------------------------------------------------------------------------

_PLACEHOLDER = "REPLACE_ME"


class SecretNotConfigured(RuntimeError):
    """Raised when a secret still holds the Terraform placeholder value."""


@cache
def get_secret(secret_id: str) -> str:
    """Fetch and cache a secret for the life of the container.

    Secrets Manager charges per API call and adds ~100ms to every invocation, so
    caching is worth it. The trade-off is that a rotated credential is not picked
    up until the container recycles; for webhook URLs and read-only API tokens
    that is acceptable. Move to the Secrets Manager Lambda extension if a tighter
    rotation SLA is ever required.
    """
    if not secret_id:
        raise SecretNotConfigured("No secret id configured")

    response = get_client("secretsmanager").get_secret_value(SecretId=secret_id)
    raw = response.get("SecretString", "")

    # Terraform seeds each secret as {"value": "..."} but an operator may well
    # paste the raw token in. Accept both.
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        value = raw
    else:
        value = parsed.get("value", raw) if isinstance(parsed, dict) else raw

    # Secrets are pasted by hand, and a stray leading or trailing space survives
    # JSON encoding invisibly. urllib3 then rejects the URL for containing a
    # control character, which reads like a network fault rather than a typo.
    value = value.strip() if isinstance(value, str) else value

    if not value or value == _PLACEHOLDER:
        raise SecretNotConfigured(
            f"Secret '{secret_id}' still holds the Terraform placeholder. "
            "Populate it before relying on this integration."
        )

    return value


# ---------------------------------------------------------------------------
# Invocation budget
#
# Enrichment runs concurrently but must not consume the whole timeout, or the
# Bedrock call gets killed mid-generation and the alert produces nothing. The
# deadline is what each collector checks before starting expensive work.
# ---------------------------------------------------------------------------


class Deadline:
    """Tracks remaining time against the Lambda's own clock."""

    def __init__(self, context: Any, reserve_seconds: float = 20.0) -> None:
        self._context = context
        self._reserve = reserve_seconds
        self._started = time.monotonic()

    def total_remaining_seconds(self) -> float:
        """Seconds left before the Lambda is killed, reserve included."""
        if self._context is not None and hasattr(self._context, "get_remaining_time_in_millis"):
            return max(0.0, self._context.get_remaining_time_in_millis() / 1000.0)
        return max(0.0, 60.0 - (time.monotonic() - self._started))

    def remaining_seconds(self) -> float:
        """Seconds left for enrichment, holding back the Bedrock reserve."""
        return max(0.0, self.total_remaining_seconds() - self._reserve)

    def expired(self) -> bool:
        return self.remaining_seconds() <= 0

    def allows(self, needed_seconds: float) -> bool:
        """Whether a step needing `needed_seconds` can still finish in time.

        Checked against the *whole* remaining budget rather than the enrichment
        share, because the caller asking is the step the reserve exists for.
        Generation is the one thing that cannot be degraded — started too late it
        is killed mid-flight, taking the whole invocation with it — so it is
        gated up front instead of attempted hopefully.
        """
        return self.total_remaining_seconds() >= needed_seconds
