"""Alert normalization tests.

The parser sits between every producer and the rest of the pipeline, so its
failure modes are the expensive ones: a mis-parsed severity changes how a page
is triaged, and an unrecognized envelope drops the alert entirely.
"""

from __future__ import annotations

import base64
import json

import pytest

import alert as alert_parser


class TestUnwrapPayload:
    def test_eventbridge_envelope(self):
        event = {
            "detail-type": "GrafanaAlert",
            "source": "grafana.alerts",
            "detail": {"alertname": "Test"},
        }
        payload, path = alert_parser.unwrap_payload(event)
        assert path == "eventbridge"
        assert payload == {"alertname": "Test"}

    def test_eventbridge_detail_as_json_string(self):
        """Some producers double-encode `detail`; the alert must still arrive."""
        event = {
            "detail-type": "GrafanaAlert",
            "source": "grafana.alerts",
            "detail": json.dumps({"alertname": "Test"}),
        }
        payload, _ = alert_parser.unwrap_payload(event)
        assert payload == {"alertname": "Test"}

    def test_function_url_body(self):
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "headers": {},
            "body": json.dumps({"alertname": "Test"}),
        }
        payload, path = alert_parser.unwrap_payload(event)
        assert path == "function_url"
        assert payload == {"alertname": "Test"}

    def test_function_url_base64_body(self):
        """Function URLs base64-encode the body for some content types."""
        event = {
            "requestContext": {"http": {"method": "POST"}},
            "headers": {},
            "body": base64.b64encode(json.dumps({"alertname": "Test"}).encode()).decode(),
            "isBase64Encoded": True,
        }
        payload, _ = alert_parser.unwrap_payload(event)
        assert payload == {"alertname": "Test"}

    def test_direct_invocation_passes_through(self):
        payload, path = alert_parser.unwrap_payload({"alertname": "Test"})
        assert path == "direct"
        assert payload == {"alertname": "Test"}

    def test_malformed_function_url_body_raises(self):
        event = {"requestContext": {}, "headers": {}, "body": "not json"}
        with pytest.raises(alert_parser.UnparseableAlert):
            alert_parser.unwrap_payload(event)


class TestGrafanaGroupParsing:
    def test_each_instance_becomes_its_own_alert(self, grafana_group_payload):
        alerts, path = alert_parser.parse(grafana_group_payload)
        assert path == "direct"
        assert len(alerts) == 2

    def test_extracts_labels_and_annotations(self, grafana_group_payload):
        alerts, _ = alert_parser.parse(grafana_group_payload)
        first = alerts[0]

        assert first.name == "HighErrorRate"
        assert first.severity == "critical"
        assert first.service == "checkout-api"
        assert first.cluster == "prod-cluster"
        assert first.runbook_url == "https://runbooks.example.com/high-error-rate"
        assert first.fingerprint == "abc123"

    def test_common_labels_merge_into_each_instance(self, grafana_group_payload):
        alerts, _ = alert_parser.parse(grafana_group_payload)
        assert alerts[0].labels["env"] == "prod"
        assert alerts[0].annotations["team"] == "payments"

    def test_instance_labels_win_over_common_labels(self):
        payload = {
            "commonLabels": {"severity": "warning"},
            "alerts": [{"status": "firing", "labels": {"alertname": "X", "severity": "critical"}}],
        }
        alerts, _ = alert_parser.parse(payload)
        assert alerts[0].severity == "critical"

    def test_resolved_instances_are_flagged_not_dropped(self, grafana_group_payload):
        """The parser reports status; suppressing resolved alerts is the
        handler's decision, so both must survive parsing."""
        alerts, _ = alert_parser.parse(grafana_group_payload)
        assert alerts[0].is_firing is True
        assert alerts[1].is_firing is False


class TestFlatPayloadParsing:
    def test_cloudwatch_alarm_shape(self):
        alerts, _ = alert_parser.parse({"AlarmName": "prod-api-5xx", "state": "ALARM"})
        assert alerts[0].name == "prod-api-5xx"

    def test_service_label_aliases(self):
        for key in ("service", "service_name", "app", "job"):
            alerts, _ = alert_parser.parse({"alertname": "X", "labels": {key: "payments"}})
            assert alerts[0].service == "payments", f"failed for label '{key}'"

    def test_payload_without_a_name_is_rejected(self):
        with pytest.raises(alert_parser.UnparseableAlert):
            alert_parser.parse({"random": "data"})

    def test_non_dict_event_is_rejected(self):
        with pytest.raises(alert_parser.UnparseableAlert):
            alert_parser.parse(["not", "a", "dict"])  # type: ignore[arg-type]


class TestAlertProperties:
    def test_dedupe_key_prefers_fingerprint(self):
        alert = alert_parser.Alert(name="X", fingerprint="fp1", service="svc")
        assert alert.dedupe_key == "fp1"

    def test_dedupe_key_falls_back_to_name_and_service(self):
        alert = alert_parser.Alert(name="X", service="svc")
        assert alert.dedupe_key == "X:svc"

    def test_age_is_none_for_unparseable_timestamp(self):
        assert alert_parser.Alert(name="X", starts_at="whenever").age_minutes() is None

    def test_age_is_computed_from_iso_timestamp(self):
        alert = alert_parser.Alert(name="X", starts_at="2020-01-01T00:00:00Z")
        age = alert.age_minutes()
        assert age is not None and age > 0

    def test_alerting_status_counts_as_firing(self):
        """Legacy Grafana emits `alerting` rather than `firing`."""
        assert alert_parser.Alert(name="X", status="alerting").is_firing is True


class TestCloudWatchAlarmParsing:
    """CloudWatch's `state` is an object, not a string. Falling through to the
    generic flat path stringifies it, fails is_firing, and silently drops the
    alarm as resolved — so this shape gets its own path."""

    def _event(self, value: str = "ALARM") -> dict:
        return {
            "source": "aws.cloudwatch",
            "detail-type": "CloudWatch Alarm State Change",
            "detail": {
                "alarmName": "banking-platform-api-5xx",
                "state": {
                    "value": value,
                    "reason": "Threshold Crossed: 3 datapoints above 10.0",
                    "timestamp": "2026-08-06T10:00:00+0000",
                },
                "configuration": {
                    "description": "API 5xx rate above threshold",
                    "metrics": [
                        {
                            "metricStat": {
                                "metric": {
                                    "dimensions": {
                                        "service": "checkout-api",
                                        "InstanceId": "i-0abc",
                                    }
                                }
                            }
                        }
                    ],
                },
            },
        }

    def test_alarm_state_is_firing(self):
        alerts, _ = alert_parser.parse(self._event("ALARM"))
        assert alerts[0].is_firing is True

    def test_ok_state_is_not_firing(self):
        alerts, _ = alert_parser.parse(self._event("OK"))
        assert alerts[0].is_firing is False

    def test_insufficient_data_is_not_firing(self):
        """Noisy by default — a metric that stopped reporting deserves its own
        alarm rather than paging through this one."""
        alerts, _ = alert_parser.parse(self._event("INSUFFICIENT_DATA"))
        assert alerts[0].is_firing is False

    def test_reason_and_description_are_extracted(self):
        alerts, _ = alert_parser.parse(self._event())
        assert "Threshold Crossed" in alerts[0].summary
        assert alerts[0].description == "API 5xx rate above threshold"

    def test_dimensions_become_labels_so_service_resolves(self):
        """Without this the alert has no service, and every enrichment source
        that needs one reports unavailable."""
        alerts, _ = alert_parser.parse(self._event())
        assert alerts[0].service == "checkout-api"
        assert alerts[0].labels["InstanceId"] == "i-0abc"

    def test_grafana_payload_still_takes_the_grafana_path(self):
        """The new branch must not shadow the existing one."""
        alerts, _ = alert_parser.parse({"alerts": [{"status": "firing", "labels": {"alertname": "X"}}]})
        assert alerts[0].name == "X" and alerts[0].is_firing
