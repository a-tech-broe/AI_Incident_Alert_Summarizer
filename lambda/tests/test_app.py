"""Handler tests.

Focused on the guarantees the pipeline is built around: authentication fails
closed, enrichment failures never suppress the notification, and a Bedrock
failure still produces a page.
"""

from __future__ import annotations

import json
import threading
from contextlib import ExitStack
from unittest.mock import patch

import app
from config import SecretNotConfigured


def _group(*services: str, name: str = "HighErrorRate") -> dict:
    """A firing group with one instance per named service."""
    return {
        "alerts": [
            {
                "status": "firing",
                "labels": {"alertname": f"{name}-{i}", "service": service, "cluster": "prod"},
                "fingerprint": f"fp{i}",
            }
            for i, service in enumerate(services)
        ]
    }


class FakeContext:
    aws_request_id = "test-request-id"

    def __init__(self, remaining_ms: int = 60000) -> None:
        self._remaining = remaining_ms

    def get_remaining_time_in_millis(self) -> int:
        return self._remaining


def _body(response: dict) -> dict:
    return json.loads(response["body"])


class TestFunctionUrlAuthentication:
    def _request(self, token: str | None) -> dict:
        headers = {"content-type": "application/json"}
        if token is not None:
            headers["X-Webhook-Token"] = token
        return {
            "requestContext": {"http": {"method": "POST", "sourceIp": "1.2.3.4"}},
            "headers": headers,
            "body": json.dumps({"alertname": "Test"}),
        }

    @patch("app.get_secret", return_value="correct-token")
    def test_rejects_a_missing_token(self, _secret):
        response = app.lambda_handler(self._request(None), FakeContext())
        assert response["statusCode"] == 401

    @patch("app.get_secret", return_value="correct-token")
    def test_rejects_an_incorrect_token(self, _secret):
        response = app.lambda_handler(self._request("wrong-token"), FakeContext())
        assert response["statusCode"] == 401

    @patch("app.get_secret", side_effect=SecretNotConfigured("placeholder"))
    def test_fails_closed_when_the_secret_is_unpopulated(self, _secret):
        """An unauthenticated public endpoint that invokes Bedrock is both a
        security and a cost problem, so an unconfigured secret must not open it."""
        response = app.lambda_handler(self._request("anything"), FakeContext())
        assert response["statusCode"] == 503

    @patch("app.get_secret", return_value="correct-token")
    def test_header_lookup_is_case_insensitive(self, _secret):
        """HTTP headers are case-insensitive and Grafana's casing is not
        guaranteed; a case-sensitive lookup would reject valid requests."""
        event = self._request(None)
        event["headers"]["x-WEBHOOK-token"] = "correct-token"

        with (
            patch("app.slack.post_summary", return_value=True),
            patch("app.prompt.summarize", return_value={"confidence": "high"}),
            patch("app._gather_context", return_value={}),
        ):
            response = app.lambda_handler(event, FakeContext())

        assert response["statusCode"] != 401

    def test_eventbridge_events_skip_the_shared_secret_check(self):
        """EventBridge is authenticated by IAM at the invoke boundary."""
        event = {
            "detail-type": "GrafanaAlert",
            "source": "grafana.alerts",
            "detail": {"alertname": "Test", "status": "resolved"},
        }
        response = app.lambda_handler(event, FakeContext())
        assert response["statusCode"] == 200


class TestAlertRouting:
    def test_unparseable_payload_returns_400(self):
        response = app.lambda_handler({"nothing": "useful"}, FakeContext())
        assert response["statusCode"] == 400

    def test_resolved_only_groups_skip_summarization(self):
        """Resolution notices carry no diagnostic value; summarizing them would
        spend a Bedrock call to restate that something recovered."""
        event = {"alerts": [{"status": "resolved", "labels": {"alertname": "X"}}]}

        with patch("app.prompt.summarize") as summarize:
            response = app.lambda_handler(event, FakeContext())

        summarize.assert_not_called()
        assert _body(response)["skipped_resolved"] == 1

    def test_only_firing_instances_are_summarized(self, grafana_group_payload):
        with (
            patch("app._gather_context", return_value={}),
            patch("app.prompt.summarize", return_value={"confidence": "high"}) as summarize,
            patch("app.slack.post_summary", return_value=True),
        ):
            response = app.lambda_handler(grafana_group_payload, FakeContext())

        assert summarize.call_count == 1
        body = _body(response)
        assert body["processed"] == 1
        assert body["skipped_resolved"] == 1


class TestFailureHandling:
    def test_bedrock_failure_still_delivers_the_raw_alert(self):
        """The failure mode of an incident tool must be 'less useful', never
        'silent'."""
        event = {"alerts": [{"status": "firing", "labels": {"alertname": "X"}}]}

        with (
            patch("app._gather_context", return_value={}),
            patch("app.prompt.summarize", side_effect=RuntimeError("throttled")),
            patch("app.slack.post_fallback", return_value=True) as fallback,
        ):
            response = app.lambda_handler(event, FakeContext())

        fallback.assert_called_once()
        result = _body(response)["results"][0]
        assert result["summarized"] is False
        assert result["delivered"] is True

    def test_a_broken_collector_does_not_lose_the_others(self):
        with (
            patch("app.ecs.collect", side_effect=RuntimeError("boom")),
            patch("app.cloudwatch.collect_alarms", return_value={"available": True}),
            patch("app.cloudwatch.collect_metrics", return_value={"available": False}),
            patch("app.cloudwatch.collect_logs", return_value={"available": False}),
            patch("app.grafana.collect", return_value={"available": False}),
            patch("app.splunk.collect", return_value={"available": False}),
        ):
            import alert as alert_parser
            from tests.conftest import FakeDeadline

            context = app._gather_context(alert_parser.Alert(name="X"), FakeDeadline())

        assert context["ecs_service"]["available"] is False
        assert "boom" in context["ecs_service"]["reason"]
        assert context["cloudwatch_alarms"]["available"] is True

    def test_delivery_failure_does_not_raise(self):
        """Returning 200 with a failure count keeps EventBridge from retrying
        the batch and re-delivering summaries that already reached Slack."""
        event = {"alerts": [{"status": "firing", "labels": {"alertname": "X"}}]}

        with (
            patch("app._gather_context", return_value={}),
            patch("app.prompt.summarize", return_value={"confidence": "low"}),
            patch("app.slack.post_summary", return_value=False),
        ):
            response = app.lambda_handler(event, FakeContext())

        body = _body(response)
        assert response["statusCode"] == 200
        assert body["delivered"] == 0
        assert body["processed"] == 1


class TestAlertGroups:
    """A Grafana group carries one entry per firing instance.

    Processed serially these overrun the timeout, and a killed invocation is not
    a return value — it bypasses the 200-with-a-failure-count contract and lets
    EventBridge retry the batch, re-posting summaries that already landed.
    """

    def test_alerts_in_a_group_are_summarized_concurrently(self):
        # A barrier is the assertion: three threads can only all arrive if the
        # alerts genuinely overlap. Serial execution deadlocks it, and the
        # timeout fails the test rather than hanging the suite.
        barrier = threading.Barrier(3, timeout=5)

        def summarize(*_args, **_kwargs):
            barrier.wait()
            return {"confidence": "high"}

        with (
            patch("app._gather_context", return_value={}),
            patch("app.prompt.summarize", side_effect=summarize),
            patch("app.slack.post_summary", return_value=True),
        ):
            response = app.lambda_handler(_group("a", "b", "c"), FakeContext())

        assert _body(response)["processed"] == 3
        assert _body(response)["delivered"] == 3

    def test_results_follow_the_order_the_producer_sent(self):
        with (
            patch("app._gather_context", return_value={}),
            patch("app.prompt.summarize", return_value={"confidence": "high"}),
            patch("app.slack.post_summary", return_value=True),
        ):
            response = app.lambda_handler(_group("a", "b", "c"), FakeContext())

        names = [r["alert"] for r in _body(response)["results"]]
        assert names == ["HighErrorRate-0", "HighErrorRate-1", "HighErrorRate-2"]

    def test_one_alert_failing_unexpectedly_does_not_sink_the_group(self):
        # Raised from delivery, which sits outside _process_alert's own try —
        # so only the group-level backstop can keep the other two alerts.
        def post_summary(alert, _summary, _context):
            if alert["name"] == "HighErrorRate-1":
                raise OSError("connection reset by peer")
            return True

        with (
            patch("app._gather_context", return_value={}),
            patch("app.prompt.summarize", return_value={"confidence": "high"}),
            patch("app.slack.post_summary", side_effect=post_summary),
        ):
            response = app.lambda_handler(_group("a", "b", "c"), FakeContext())

        body = _body(response)
        assert response["statusCode"] == 200
        assert body["processed"] == 3
        assert body["delivered"] == 2


class TestSharedEnrichment:
    """Alerts in a group share commonLabels, so their collectors would otherwise
    re-issue identical calls — including the Insights query, the slowest and the
    only one billed per scan."""

    def _run(self, event):
        """Run a group with every collector stubbed, recording log lookups.

        Returns the service argument of each `collect_logs` call — the Insights
        query is the collector worth counting.
        """
        calls: list[str] = []

        def collect_logs(service, _deadline):
            calls.append(service)
            return {"available": True}

        stubs = {
            "app.ecs.collect": {"available": False},
            "app.cloudwatch.collect_alarms": {"available": False},
            "app.cloudwatch.collect_metrics": {"available": False},
            "app.grafana.collect": {"available": False},
            "app.splunk.collect": {"available": False},
        }

        with ExitStack() as stack:
            for target, value in stubs.items():
                stack.enter_context(patch(target, return_value=value))
            stack.enter_context(patch("app.cloudwatch.collect_logs", collect_logs))
            stack.enter_context(patch("app.prompt.summarize", return_value={"confidence": "high"}))
            stack.enter_context(patch("app.slack.post_summary", return_value=True))

            app.lambda_handler(event, FakeContext())

        return calls

    def test_identical_lookups_run_once_for_the_whole_group(self):
        assert self._run(_group("checkout", "checkout", "checkout")) == ["checkout"]

    def test_different_services_still_get_their_own_lookups(self):
        assert sorted(self._run(_group("checkout", "payments"))) == ["checkout", "payments"]


class TestGenerationBudget:
    def test_an_alert_with_no_time_left_is_delivered_raw(self):
        """Starting a generation that cannot finish spends the model call and
        still loses the alert. The raw page is worth more."""
        event = {"alerts": [{"status": "firing", "labels": {"alertname": "X"}}]}

        with (
            patch("app.prompt.summarize") as summarize,
            patch("app.slack.post_fallback", return_value=True) as fallback,
        ):
            # Under _ALERT_BUDGET_SECONDS: too little for generation + delivery.
            response = app.lambda_handler(event, FakeContext(remaining_ms=10_000))

        summarize.assert_not_called()
        fallback.assert_called_once()
        assert _body(response)["results"][0]["delivered"] is True

    def test_enrichment_is_skipped_too_when_the_budget_is_gone(self):
        """The check sits ahead of the gather stage, so a doomed alert does not
        pay for context it will never summarize."""
        event = {"alerts": [{"status": "firing", "labels": {"alertname": "X"}}]}

        with (
            patch("app._gather_context") as gather,
            patch("app.slack.post_fallback", return_value=True),
        ):
            app.lambda_handler(event, FakeContext(remaining_ms=10_000))

        gather.assert_not_called()

    def test_a_normal_budget_still_summarizes(self):
        event = {"alerts": [{"status": "firing", "labels": {"alertname": "X"}}]}

        with (
            patch("app._gather_context", return_value={}),
            patch("app.prompt.summarize", return_value={"confidence": "high"}) as summarize,
            patch("app.slack.post_summary", return_value=True),
        ):
            app.lambda_handler(event, FakeContext(remaining_ms=60_000))

        summarize.assert_called_once()


class TestSafeCollect:
    def test_wraps_exceptions_as_unavailable(self):
        result = app._safe_collect("test", lambda: 1 / 0)
        assert result["available"] is False

    def test_rejects_a_non_dict_return_value(self):
        result = app._safe_collect("test", lambda: "unexpected")
        assert result["available"] is False
