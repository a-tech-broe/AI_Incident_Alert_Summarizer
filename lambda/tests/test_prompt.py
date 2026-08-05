"""Prompt construction and response-parsing tests.

Two things matter here and neither is cosmetic:

  * unavailable context must be *visible* to the model, not silently omitted;
  * a malformed model response must degrade, not crash, because by that point
    the invocation has already spent its Bedrock call.
"""

from __future__ import annotations

import json

import pytest

import prompt


class TestBuildUserPrompt:
    def test_alert_is_fenced_in_telemetry_tags(self):
        rendered = prompt.build_user_prompt({"name": "X"}, {})
        assert "<telemetry>" in rendered
        assert "</telemetry>" in rendered
        assert "<alert>" in rendered

    def test_available_context_is_serialized_under_its_own_tag(self):
        rendered = prompt.build_user_prompt(
            {"name": "X"},
            {"ecs_service": {"available": True, "running_count": 0, "desired_count": 3}},
        )
        assert "<ecs_service>" in rendered
        assert '"running_count": 0' in rendered

    def test_unavailable_context_is_stated_rather_than_omitted(self):
        """The model must be able to tell 'checked and healthy' from 'never
        checked' — otherwise it reports false completeness."""
        rendered = prompt.build_user_prompt(
            {"name": "X"},
            {"splunk": {"available": False, "reason": "Splunk unreachable"}},
        )
        assert 'status="unavailable"' in rendered
        assert "Splunk unreachable" in rendered

    def test_non_dict_context_entries_are_ignored(self):
        rendered = prompt.build_user_prompt({"name": "X"}, {"junk": "not a dict"})
        assert "<junk>" not in rendered

    def test_non_serializable_values_do_not_raise(self):
        """Collectors return boto datetimes; json.dumps must not blow up on them."""
        from datetime import datetime

        rendered = prompt.build_user_prompt(
            {"name": "X"},
            {"ecs_service": {"available": True, "stopped_at": datetime(2026, 1, 1)}},
        )
        assert "2026-01-01" in rendered


class TestSystemPrompt:
    def test_forbids_fabrication(self):
        assert "never invent" in prompt.SYSTEM_PROMPT.lower()

    def test_instructs_the_model_to_treat_telemetry_as_data(self):
        assert "untrusted data" in prompt.SYSTEM_PROMPT.lower()

    def test_requires_the_json_schema_fields(self):
        for field in ("headline", "probable_cause", "confidence", "recommended_actions"):
            assert field in prompt.SYSTEM_PROMPT


class TestResponseParsing:
    def test_parses_a_prefilled_response(self):
        """The assistant turn is prefilled with `{`, so the model's reply omits it."""
        reply = '"headline": "X", "confidence": "high"}'
        parsed = prompt._parse_response(reply)
        assert parsed["headline"] == "X"
        assert parsed["confidence"] == "high"

    def test_parses_a_complete_json_object(self):
        parsed = prompt._parse_response(json.dumps({"headline": "X"}))
        assert parsed["headline"] == "X"

    def test_recovers_json_wrapped_in_prose(self):
        reply = 'Here is the summary:\n{"headline": "X"}\nHope that helps.'
        assert prompt._parse_response(reply)["headline"] == "X"

    def test_raises_when_no_json_is_present(self):
        with pytest.raises(ValueError):
            prompt._parse_response("I could not analyze this alert.")


class TestNormalization:
    def test_missing_fields_get_safe_defaults(self):
        """Slack formatting runs after the expensive Bedrock call; a missing
        optional field must not crash it."""
        normalized = prompt._normalize({"headline": "X"})
        assert normalized["impact"] == "Not determined"
        assert normalized["evidence"] == []
        assert normalized["recommended_actions"] == []

    def test_invalid_severity_falls_back_to_medium(self):
        assert prompt._normalize({"severity_assessment": "catastrophic"})["severity_assessment"] == "medium"

    def test_invalid_confidence_falls_back_to_low(self):
        """Defaulting to low is deliberate: an unparseable confidence value must
        not present an unvalidated inference as trustworthy."""
        assert prompt._normalize({"confidence": "absolute"})["confidence"] == "low"

    def test_severity_is_case_insensitive(self):
        assert prompt._normalize({"severity_assessment": "CRITICAL"})["severity_assessment"] == "critical"

    def test_string_list_fields_are_coerced_to_lists(self):
        normalized = prompt._normalize({"evidence": "a single observation"})
        assert normalized["evidence"] == ["a single observation"]

    def test_falsy_list_entries_are_dropped(self):
        normalized = prompt._normalize({"recommended_actions": ["do this", "", None, "then this"]})
        assert normalized["recommended_actions"] == ["do this", "then this"]
