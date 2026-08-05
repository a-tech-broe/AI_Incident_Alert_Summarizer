"""Collector tests.

The contract every collector must honour: return a dict, never raise, and be
honest about what it could not gather. Everything downstream — the prompt, the
Slack footer, the context_gaps field — depends on that.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError

import cloudwatch
import ecs
import slack
import splunk


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": code}}, "Operation")


class TestEcsCollector:
    def test_no_service_label_reports_unavailable(self, deadline):
        result = ecs.collect(None, None, deadline)
        assert result["available"] is False
        assert "service label" in result["reason"]

    def test_expired_deadline_skips_the_call(self, expired_deadline):
        result = ecs.collect("svc", "cluster", expired_deadline)
        assert result["available"] is False
        assert "deadline" in result["reason"]

    def test_api_error_is_reported_not_raised(self, deadline):
        with patch("ecs.get_client") as get_client:
            get_client.return_value.describe_services.side_effect = _client_error("AccessDeniedException")
            result = ecs.collect("svc", "cluster", deadline)

        assert result["available"] is False
        assert "AccessDeniedException" in result["reason"]

    def test_cluster_arn_label_is_reduced_to_a_name(self, deadline):
        client = MagicMock()
        client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "svc",
                    "status": "ACTIVE",
                    "desiredCount": 2,
                    "runningCount": 2,
                    "taskDefinition": "arn:aws:ecs:us-east-1:1:task-definition/svc:5",
                    "deployments": [],
                    "events": [],
                }
            ]
        }

        with patch("ecs.get_client", return_value=client):
            result = ecs.collect("svc", "arn:aws:ecs:us-east-1:1:cluster/prod", deadline)

        assert result["cluster"] == "prod"
        client.describe_services.assert_called_with(cluster="prod", services=["svc"])

    def test_stopped_tasks_are_only_fetched_when_capacity_is_short(self, deadline):
        client = MagicMock()
        client.describe_services.return_value = {
            "services": [
                {
                    "serviceName": "svc",
                    "status": "ACTIVE",
                    "desiredCount": 3,
                    "runningCount": 3,
                    "taskDefinition": "td",
                    "deployments": [],
                    "events": [],
                }
            ]
        }

        with patch("ecs.get_client", return_value=client):
            result = ecs.collect("svc", "prod", deadline)

        client.list_tasks.assert_not_called()
        assert "stopped_tasks" not in result


class TestEcsHealthSummary:
    def test_reports_a_full_outage(self):
        summary = ecs._health_summary({"desired_count": 3, "running_count": 0, "deployments": []})
        assert "fully down" in summary

    def test_reports_partial_degradation(self):
        summary = ecs._health_summary({"desired_count": 3, "running_count": 1, "deployments": []})
        assert "degraded" in summary

    def test_an_in_progress_rollout_takes_precedence(self):
        """A rollout is the most common cause of a sudden alert; saying
        'degraded' would send the responder looking for a fault that is really
        just a deployment in flight."""
        summary = ecs._health_summary(
            {
                "desired_count": 3,
                "running_count": 1,
                "deployments": [{"status": "PRIMARY", "rollout_state": "IN_PROGRESS"}],
            }
        )
        assert "Deployment in progress" in summary


class TestCloudWatchHelpers:
    def test_trend_detects_a_rise(self):
        # Newest-first: a recent spike over a flat baseline.
        assert "rising" in cloudwatch._trend([90.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0])

    def test_trend_reports_flat_for_steady_values(self):
        assert cloudwatch._trend([50.0] * 8) == "flat"

    def test_trend_needs_enough_samples(self):
        assert cloudwatch._trend([50.0, 51.0]) == "insufficient data"

    def test_series_summary_uses_the_newest_value_as_latest(self):
        summary = cloudwatch._describe_series([90.0, 20.0, 20.0, 20.0], [95.0])
        assert summary["latest_percent"] == 90.0
        assert summary["peak_percent"] == 95.0

    def test_empty_series_reports_unavailable(self):
        assert cloudwatch._describe_series([], [])["available"] is False

    def test_alarm_relatedness_matches_on_service_name(self):
        alarm = {"AlarmName": "checkout-api-5xx", "Dimensions": []}
        assert cloudwatch._is_related(alarm, "SomeAlert", "checkout-api") is True

    def test_generic_tokens_do_not_match_everything(self):
        """Without the stop-word filter, 'HighErrorRate' would mark every alarm
        containing 'error' as related and inflate the blast radius."""
        alarm = {"AlarmName": "unrelated-error-alarm", "Dimensions": []}
        assert cloudwatch._is_related(alarm, "High Error Rate", None) is False


class TestSplunkCollector:
    def test_disabled_when_unconfigured(self, deadline):
        result = splunk.collect("svc", deadline)
        assert result["available"] is False

    def test_sanitizer_strips_spl_metacharacters(self):
        """Alert labels come from outside the system: an unfiltered pipe would
        append arbitrary commands to the search."""
        assert "|" not in splunk._sanitize("svc | delete index=*")
        assert "`" not in splunk._sanitize("svc `whoami`")

    def test_sanitizer_keeps_legitimate_service_names(self):
        assert splunk._sanitize("checkout-api_v2.prod") == "checkout-api_v2.prod"

    def test_export_parser_skips_progress_lines(self):
        body = "\n".join(
            [
                '{"preview": true}',
                '{"result": {"source": "app.log", "occurrences": "5", "punct": "..."}}',
                "not json at all",
                '{"result": {"source": "err.log", "occurrences": "2"}}',
            ]
        )
        rows = splunk._parse_export(body)
        assert len(rows) == 2
        assert rows[0]["occurrences"] == 5


class TestSlackFormatting:
    def test_summary_message_is_valid_block_kit(self, model_summary):
        blocks = slack._build_blocks({"name": "X", "severity": "critical"}, model_summary, {})
        assert blocks[0]["type"] == "header"
        assert all("type" in block for block in blocks)

    def test_header_stays_within_slack_limits(self, model_summary):
        model_summary["headline"] = "x" * 500
        blocks = slack._build_blocks({"name": "X"}, model_summary, {})
        assert len(blocks[0]["text"]["text"]) <= 150

    def test_confidence_is_surfaced_next_to_the_cause(self, model_summary):
        """Presenting an inference with the same weight as a metric reading is
        how an automated summary starts costing more time than it saves."""
        blocks = slack._build_blocks({"name": "X"}, model_summary, {})
        rendered = str(blocks)
        assert "Medium confidence" in rendered

    def test_long_lists_are_capped_with_a_remainder_note(self):
        section = slack._bullet_section("Evidence", [f"item {i}" for i in range(20)])
        assert "and 14 more" in section["text"]["text"]

    def test_link_block_is_omitted_when_there_are_no_links(self):
        assert slack._link_block({"name": "X"}) is None

    def test_link_block_renders_available_links(self):
        block = slack._link_block({"runbook_url": "https://r", "dashboard_url": "https://d"})
        assert "Runbook" in str(block) and "Dashboard" in str(block)

    def test_sources_line_reports_when_nothing_was_gathered(self):
        assert slack._sources_line({}) == "alert payload only"

    def test_sources_line_lists_only_available_sources(self):
        line = slack._sources_line({"ecs_service": {"available": True}, "splunk": {"available": False}})
        assert "ecs service" in line and "splunk" not in line


class TestCredentialRedaction:
    """A Slack webhook's path is its credential — it must never reach a log."""

    def test_redact_strips_the_path(self):
        import http_client

        assert (
            http_client._redact("https://hooks.slack.com/services/T0/B0/secret")
            == "https://hooks.slack.com/…"
        )

    def test_scrub_removes_url_embedded_in_exception_text(self):
        import http_client

        # The exact shape urllib3 produces, which previously leaked verbatim.
        raw = (
            "HTTPConnectionPool(host='hooks.slack.com', port=443): Max retries "
            "exceeded with url: //hooks.slack.com/services/T0/B0/SUPERSECRET"
        )
        scrubbed = http_client._scrub(raw)
        assert "SUPERSECRET" not in scrubbed
        assert "hooks.slack.com" in scrubbed

    def test_scrub_removes_the_known_path_even_when_malformed(self):
        """A space-corrupted URL defeats the regex; the literal path match is
        the backstop, because that path is the credential."""
        import http_client

        target = "https://hooks.slack.com/services/T0/B0/SUPERSECRET"
        raw = "InvalidURL: ' https'  // hooks.slack.com/services/T0/B0/SUPERSECRET"
        assert "SUPERSECRET" not in http_client._scrub(raw, target)

    def test_secret_values_are_stripped(self):
        """A pasted leading space survives JSON encoding invisibly."""
        import json as _json
        from unittest.mock import MagicMock, patch

        import config

        client = MagicMock()
        client.get_secret_value.return_value = {
            "SecretString": _json.dumps({"value": "  https://hooks.slack.com/services/x  "})
        }
        config.get_secret.cache_clear()
        with patch("config.get_client", return_value=client):
            assert config.get_secret("test/id") == "https://hooks.slack.com/services/x"
        config.get_secret.cache_clear()
