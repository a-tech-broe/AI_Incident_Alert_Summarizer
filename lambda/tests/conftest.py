"""Shared test fixtures.

Environment variables are set before any handler module is imported: `Config`
reads them at class-definition time via dataclass defaults, so importing first
and setting them later would bake in the wrong values.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

LAMBDA_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(LAMBDA_DIR))

os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("PROJECT_NAME", "ai-incident-summarizer")
os.environ.setdefault("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
os.environ.setdefault("BEDROCK_MAX_TOKENS", "2000")
os.environ.setdefault("BEDROCK_TEMPERATURE", "0.2")
os.environ.setdefault("CONTEXT_LOOKBACK_MINUTES", "30")
os.environ.setdefault("SLACK_WEBHOOK_SECRET_ID", "test/slack")
os.environ.setdefault("WEBHOOK_TOKEN_SECRET_ID", "test/webhook")


@pytest.fixture
def grafana_group_payload() -> dict:
    """A Grafana unified-alerting webhook body with two firing instances."""
    return {
        "receiver": "aws-summarizer",
        "status": "firing",
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "alertname": "HighErrorRate",
                    "severity": "critical",
                    "service": "checkout-api",
                    "cluster": "prod-cluster",
                },
                "annotations": {
                    "summary": "Error rate above 5% for 10 minutes",
                    "runbook_url": "https://runbooks.example.com/high-error-rate",
                },
                "startsAt": "2026-08-04T10:00:00Z",
                "fingerprint": "abc123",
                "generatorURL": "https://grafana.example.com/alerting/grafana/rule",
                "dashboardURL": "https://grafana.example.com/d/abc",
                "values": {"A": 12.4},
            },
            {
                "status": "resolved",
                "labels": {"alertname": "HighLatency", "severity": "warning", "service": "checkout-api"},
                "annotations": {"summary": "p99 latency recovered"},
                "startsAt": "2026-08-04T09:30:00Z",
                "fingerprint": "def456",
            },
        ],
        "commonLabels": {"env": "prod"},
        "commonAnnotations": {"team": "payments"},
        "externalURL": "https://grafana.example.com",
        "truncatedAlerts": 0,
    }


@pytest.fixture
def model_summary() -> dict:
    return {
        "headline": "checkout-api returning 5xx after a deployment",
        "severity_assessment": "critical",
        "impact": "Checkout requests are failing for a subset of users.",
        "probable_cause": "A deployment 8 minutes ago coincides with the error onset.",
        "confidence": "medium",
        "evidence": ["Error rate rose from 0.1% to 12.4%", "ECS rollout IN_PROGRESS"],
        "recommended_actions": ["Check the rollout status", "Roll back if errors persist"],
        "context_gaps": ["Splunk was unreachable"],
    }


class FakeDeadline:
    """Deadline stand-in with a fixed remaining budget.

    `remaining_seconds` is the enrichment share; `total_remaining_seconds`
    adds back the reserve the real Deadline holds for generation, so a fixture
    built for the collectors still answers `allows()` sensibly.
    """

    def __init__(self, seconds: float = 30.0, reserve: float = 22.0) -> None:
        self._seconds = seconds
        self._reserve = reserve

    def remaining_seconds(self) -> float:
        return self._seconds

    def total_remaining_seconds(self) -> float:
        return self._seconds + self._reserve

    def expired(self) -> bool:
        return self._seconds <= 0

    def allows(self, needed_seconds: float) -> bool:
        return self.total_remaining_seconds() >= needed_seconds


@pytest.fixture
def deadline() -> FakeDeadline:
    return FakeDeadline()


@pytest.fixture
def expired_deadline() -> FakeDeadline:
    return FakeDeadline(seconds=0.0)
