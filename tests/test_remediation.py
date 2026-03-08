"""Tests for the Remediation Engine (supervisor/remediation.py).

Covers:
- Template lookup for all 10 incident types
- YAML override loading and merging
- LLM enrichment (mock converse)
- LLM failure graceful degradation
- Structured output schema validation
- Warning generation for low confidence
- Warning generation for high-risk actions
- verify_before_acting enforcement
- Missing YAML file handling
- Invalid YAML handling
- Template with ITSM context (rollback plan from ServiceNow)
- Template with DevOps context (PR number, deployment version)
- Risk level validation
- Confidence scoring for remediation
- Edge cases: empty root cause, unknown incident type
"""

from __future__ import annotations

import copy
import json
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from supervisor.remediation import (
    REMEDIATION_TEMPLATES,
    VALID_INCIDENT_TYPES,
    _load_yaml_overrides,
    enrich_remediation_llm,
    generate_remediation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# All 10 incident types the engine must cover
ALL_INCIDENT_TYPES = [
    "timeout",
    "oomkill",
    "error_spike",
    "latency",
    "saturation",
    "network",
    "cascading",
    "missing_data",
    "flapping",
    "silent_failure",
]

# Required keys in every generate_remediation output
REQUIRED_OUTPUT_KEYS = {
    "immediate_actions",
    "permanent_fix",
    "risk_level",
    "confidence",
    "verify_before_acting",
    "warnings",
    "source",
    "runbook_hint",
}

# Required keys inside each code-default template
REQUIRED_TEMPLATE_KEYS = {
    "immediate_actions",
    "permanent_fix",
    "risk_level",
    "verify_before_acting",
    "runbook_hint",
}

VALID_RISK_LEVELS = {"low", "medium", "high", "critical"}


def _make_enriched_json(**overrides: Any) -> str:
    """Build a valid enriched-template JSON string for mock LLM responses."""
    base = {
        "immediate_actions": ["Rollback payment-svc v2.3.1 (PR #847)"],
        "permanent_fix": ["Add memory profiling to CI pipeline"],
        "risk_level": "high",
        "verify_before_acting": True,
        "runbook_hint": "runbook/oomkill-response.md",
    }
    base.update(overrides)
    return json.dumps(base)


def _mock_converse_success(text: str) -> dict[str, Any]:
    """Return a mock converse() success response."""
    return {
        "text": text,
        "input_tokens": 200,
        "output_tokens": 100,
        "model_id": "test-model",
        "latency_ms": 42.0,
        "stop_reason": "end_turn",
    }


def _mock_converse_error() -> dict[str, Any]:
    """Return a mock converse() error response."""
    return {
        "text": "",
        "error": "bedrock_error: ThrottlingException",
        "input_tokens": 0,
        "output_tokens": 0,
        "model_id": "test-model",
        "latency_ms": 10.0,
        "stop_reason": "error",
    }


# ---------------------------------------------------------------------------
# 1. Template lookup for all 10 incident types
# ---------------------------------------------------------------------------

class TestTemplateLookup:
    """Verify every incident type has a well-formed code-default template."""

    @pytest.mark.parametrize("incident_type", ALL_INCIDENT_TYPES)
    def test_template_exists_for_each_type(self, incident_type: str):
        assert incident_type in REMEDIATION_TEMPLATES

    @pytest.mark.parametrize("incident_type", ALL_INCIDENT_TYPES)
    def test_template_has_required_keys(self, incident_type: str):
        template = REMEDIATION_TEMPLATES[incident_type]
        assert REQUIRED_TEMPLATE_KEYS.issubset(template.keys()), (
            f"Template '{incident_type}' missing keys: "
            f"{REQUIRED_TEMPLATE_KEYS - template.keys()}"
        )

    @pytest.mark.parametrize("incident_type", ALL_INCIDENT_TYPES)
    def test_template_risk_level_is_valid(self, incident_type: str):
        assert REMEDIATION_TEMPLATES[incident_type]["risk_level"] in VALID_RISK_LEVELS

    @pytest.mark.parametrize("incident_type", ALL_INCIDENT_TYPES)
    def test_immediate_actions_is_nonempty_list(self, incident_type: str):
        actions = REMEDIATION_TEMPLATES[incident_type]["immediate_actions"]
        assert isinstance(actions, list) and len(actions) > 0

    @pytest.mark.parametrize("incident_type", ALL_INCIDENT_TYPES)
    def test_permanent_fix_is_nonempty_list(self, incident_type: str):
        fixes = REMEDIATION_TEMPLATES[incident_type]["permanent_fix"]
        assert isinstance(fixes, list) and len(fixes) > 0

    def test_valid_incident_types_matches_template_keys(self):
        assert VALID_INCIDENT_TYPES == set(REMEDIATION_TEMPLATES.keys())

    def test_exactly_ten_incident_types(self):
        assert len(REMEDIATION_TEMPLATES) == 10


# ---------------------------------------------------------------------------
# 2. YAML override loading and merging
# ---------------------------------------------------------------------------

class TestYAMLOverrideLoading:
    """Verify _load_yaml_overrides correctly merges YAML with code defaults."""

    def test_missing_yaml_file_returns_code_defaults(self, tmp_path: Path):
        nonexistent = tmp_path / "does_not_exist.yaml"
        result = _load_yaml_overrides(yaml_path=nonexistent)
        assert result == REMEDIATION_TEMPLATES

    def test_empty_yaml_file_returns_code_defaults(self, tmp_path: Path):
        yaml_file = tmp_path / "empty.yaml"
        yaml_file.write_text("")
        result = _load_yaml_overrides(yaml_path=yaml_file)
        # yaml.safe_load("") returns None, which is not a dict
        assert result == REMEDIATION_TEMPLATES

    def test_invalid_yaml_syntax_returns_code_defaults(self, tmp_path: Path):
        yaml_file = tmp_path / "bad.yaml"
        yaml_file.write_text(":::invalid yaml [[[")
        result = _load_yaml_overrides(yaml_path=yaml_file)
        assert result == REMEDIATION_TEMPLATES

    def test_yaml_not_a_dict_returns_code_defaults(self, tmp_path: Path):
        yaml_file = tmp_path / "list.yaml"
        yaml_file.write_text("- item1\n- item2\n")
        result = _load_yaml_overrides(yaml_path=yaml_file)
        assert result == REMEDIATION_TEMPLATES

    def test_yaml_overrides_not_a_dict_returns_code_defaults(self, tmp_path: Path):
        yaml_file = tmp_path / "bad_overrides.yaml"
        yaml_file.write_text("overrides: 'not a dict'\n")
        result = _load_yaml_overrides(yaml_path=yaml_file)
        assert result == REMEDIATION_TEMPLATES

    def test_yaml_overrides_single_field(self, tmp_path: Path):
        yaml_file = tmp_path / "override.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            overrides:
              timeout:
                risk_level: "critical"
        """))
        result = _load_yaml_overrides(yaml_path=yaml_file)
        assert result["timeout"]["risk_level"] == "critical"
        # Other fields should remain from code defaults
        assert result["timeout"]["immediate_actions"] == [
            "Check downstream dependencies",
            "Increase timeout thresholds",
        ]

    def test_yaml_overrides_immediate_actions(self, tmp_path: Path):
        yaml_file = tmp_path / "override.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            overrides:
              oomkill:
                immediate_actions:
                  - "Custom action 1"
                  - "Custom action 2"
        """))
        result = _load_yaml_overrides(yaml_path=yaml_file)
        assert result["oomkill"]["immediate_actions"] == [
            "Custom action 1",
            "Custom action 2",
        ]
        # permanent_fix should remain from code defaults
        assert result["oomkill"]["permanent_fix"] == REMEDIATION_TEMPLATES["oomkill"]["permanent_fix"]

    def test_yaml_adds_new_incident_type(self, tmp_path: Path):
        yaml_file = tmp_path / "override.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            overrides:
              custom_type:
                immediate_actions:
                  - "Do custom thing"
                risk_level: "low"
        """))
        result = _load_yaml_overrides(yaml_path=yaml_file)
        assert "custom_type" in result
        assert result["custom_type"]["immediate_actions"] == ["Do custom thing"]
        assert result["custom_type"]["risk_level"] == "low"
        # Scaffold defaults for fields not provided in YAML
        assert result["custom_type"]["permanent_fix"] == []
        assert result["custom_type"]["verify_before_acting"] is False
        assert result["custom_type"]["runbook_hint"] == ""

    def test_yaml_non_dict_incident_type_skipped(self, tmp_path: Path):
        yaml_file = tmp_path / "override.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            overrides:
              timeout: "not a dict"
              oomkill:
                risk_level: "critical"
        """))
        result = _load_yaml_overrides(yaml_path=yaml_file)
        # timeout should remain unchanged (non-dict skipped)
        assert result["timeout"] == REMEDIATION_TEMPLATES["timeout"]
        # oomkill should be overridden
        assert result["oomkill"]["risk_level"] == "critical"

    def test_yaml_does_not_mutate_code_defaults(self, tmp_path: Path):
        original = copy.deepcopy(REMEDIATION_TEMPLATES)
        yaml_file = tmp_path / "override.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            overrides:
              timeout:
                risk_level: "critical"
                immediate_actions:
                  - "Override action"
        """))
        _load_yaml_overrides(yaml_path=yaml_file)
        # Code defaults must not have been mutated
        assert REMEDIATION_TEMPLATES == original

    def test_yaml_multiple_type_overrides(self, tmp_path: Path):
        yaml_file = tmp_path / "override.yaml"
        yaml_file.write_text(textwrap.dedent("""\
            overrides:
              timeout:
                risk_level: "high"
              latency:
                risk_level: "critical"
              flapping:
                verify_before_acting: true
        """))
        result = _load_yaml_overrides(yaml_path=yaml_file)
        assert result["timeout"]["risk_level"] == "high"
        assert result["latency"]["risk_level"] == "critical"
        assert result["flapping"]["verify_before_acting"] is True


# ---------------------------------------------------------------------------
# 3. LLM enrichment (mock converse)
# ---------------------------------------------------------------------------

class TestLLMEnrichment:
    """Verify enrich_remediation_llm integrates with converse() correctly."""

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    def test_returns_template_when_llm_disabled(self, mock_enabled: MagicMock):
        template = copy.deepcopy(REMEDIATION_TEMPLATES["timeout"])
        result = enrich_remediation_llm(template, "root cause", "evidence")
        assert result is template

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    def test_enriches_template_with_llm(
        self, mock_enabled: MagicMock, mock_converse: MagicMock
    ):
        enriched_text = _make_enriched_json()
        mock_converse.return_value = _mock_converse_success(enriched_text)

        template = copy.deepcopy(REMEDIATION_TEMPLATES["oomkill"])
        result = enrich_remediation_llm(
            template, "memory leak in payment-svc", "Pod restarted 3 times"
        )

        assert result["immediate_actions"] == ["Rollback payment-svc v2.3.1 (PR #847)"]
        assert result["risk_level"] == "high"
        mock_converse.assert_called_once()
        # Verify temperature=0.0 is passed
        call_kwargs = mock_converse.call_args
        assert call_kwargs.kwargs.get("temperature") == 0.0 or call_kwargs[1].get("temperature") == 0.0

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    def test_includes_itsm_context_in_prompt(
        self, mock_enabled: MagicMock, mock_converse: MagicMock
    ):
        mock_converse.return_value = _mock_converse_success(_make_enriched_json())
        template = copy.deepcopy(REMEDIATION_TEMPLATES["oomkill"])

        itsm = {"rollback_plan": "revert PR #847", "change_id": "CHG001234"}
        enrich_remediation_llm(
            template, "memory leak", "evidence", itsm_context=itsm
        )

        user_msg = mock_converse.call_args[1].get("user_message") or mock_converse.call_args[0][1]
        assert "ITSM context" in user_msg
        assert "PR #847" in user_msg

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    def test_includes_devops_context_in_prompt(
        self, mock_enabled: MagicMock, mock_converse: MagicMock
    ):
        mock_converse.return_value = _mock_converse_success(_make_enriched_json())
        template = copy.deepcopy(REMEDIATION_TEMPLATES["oomkill"])

        devops = {"deployment_version": "v2.3.1", "pr_number": 847}
        enrich_remediation_llm(
            template, "memory leak", "evidence", devops_context=devops
        )

        user_msg = mock_converse.call_args[1].get("user_message") or mock_converse.call_args[0][1]
        assert "DevOps context" in user_msg
        assert "v2.3.1" in user_msg

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    def test_strips_markdown_code_fences(
        self, mock_enabled: MagicMock, mock_converse: MagicMock
    ):
        fenced = "```json\n" + _make_enriched_json() + "\n```"
        mock_converse.return_value = _mock_converse_success(fenced)

        template = copy.deepcopy(REMEDIATION_TEMPLATES["oomkill"])
        result = enrich_remediation_llm(template, "root cause", "evidence")
        assert isinstance(result["immediate_actions"], list)
        assert result["risk_level"] in VALID_RISK_LEVELS


# ---------------------------------------------------------------------------
# 4. LLM failure graceful degradation
# ---------------------------------------------------------------------------

class TestLLMGracefulDegradation:
    """Verify fallback to template when LLM fails in various ways."""

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    def test_returns_template_on_converse_error(
        self, mock_enabled: MagicMock, mock_converse: MagicMock
    ):
        mock_converse.return_value = _mock_converse_error()
        template = copy.deepcopy(REMEDIATION_TEMPLATES["timeout"])
        result = enrich_remediation_llm(template, "root cause", "evidence")
        assert result is template

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    def test_returns_template_on_empty_text(
        self, mock_enabled: MagicMock, mock_converse: MagicMock
    ):
        mock_converse.return_value = _mock_converse_success("")
        template = copy.deepcopy(REMEDIATION_TEMPLATES["timeout"])
        result = enrich_remediation_llm(template, "root cause", "evidence")
        assert result is template

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    def test_returns_template_on_invalid_json(
        self, mock_enabled: MagicMock, mock_converse: MagicMock
    ):
        mock_converse.return_value = _mock_converse_success("not json at all {{{")
        template = copy.deepcopy(REMEDIATION_TEMPLATES["timeout"])
        result = enrich_remediation_llm(template, "root cause", "evidence")
        assert result is template

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    def test_returns_template_on_missing_required_keys(
        self, mock_enabled: MagicMock, mock_converse: MagicMock
    ):
        incomplete = json.dumps({"immediate_actions": ["do something"]})
        mock_converse.return_value = _mock_converse_success(incomplete)
        template = copy.deepcopy(REMEDIATION_TEMPLATES["timeout"])
        result = enrich_remediation_llm(template, "root cause", "evidence")
        assert result is template

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    def test_returns_template_on_invalid_risk_level(
        self, mock_enabled: MagicMock, mock_converse: MagicMock
    ):
        bad_risk = _make_enriched_json(risk_level="ultra_critical")
        mock_converse.return_value = _mock_converse_success(bad_risk)
        template = copy.deepcopy(REMEDIATION_TEMPLATES["timeout"])
        result = enrich_remediation_llm(template, "root cause", "evidence")
        assert result is template

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    def test_returns_template_on_converse_exception(
        self, mock_enabled: MagicMock, mock_converse: MagicMock
    ):
        mock_converse.side_effect = RuntimeError("Connection timeout")
        template = copy.deepcopy(REMEDIATION_TEMPLATES["timeout"])
        result = enrich_remediation_llm(template, "root cause", "evidence")
        assert result is template


# ---------------------------------------------------------------------------
# 5. Structured output schema validation
# ---------------------------------------------------------------------------

class TestStructuredOutput:
    """Verify generate_remediation output matches the expected schema."""

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_output_has_all_required_keys(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="slow database",
            confidence=85,
            evidence_summary="Query latency > 5s",
        )
        assert REQUIRED_OUTPUT_KEYS.issubset(result.keys())

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_output_types_are_correct(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="slow database",
            confidence=85,
            evidence_summary="Query latency > 5s",
        )
        assert isinstance(result["immediate_actions"], list)
        assert isinstance(result["permanent_fix"], list)
        assert isinstance(result["risk_level"], str)
        assert isinstance(result["confidence"], (int, float))
        assert isinstance(result["verify_before_acting"], bool)
        assert isinstance(result["warnings"], list)
        assert isinstance(result["source"], str)
        assert isinstance(result["runbook_hint"], str)

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_source_is_template_only_when_llm_disabled(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="slow db",
            confidence=80,
            evidence_summary="evidence",
        )
        assert result["source"] == "template_only"

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_source_is_template_plus_llm_when_enriched(
        self, mock_yaml: MagicMock, mock_llm: MagicMock, mock_converse: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        mock_converse.return_value = _mock_converse_success(_make_enriched_json())
        result = generate_remediation(
            incident_type="oomkill",
            root_cause="memory leak",
            confidence=80,
            evidence_summary="evidence",
        )
        assert result["source"] == "template+llm"

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_confidence_passed_through(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="root cause",
            confidence=73.5,
            evidence_summary="evidence",
        )
        assert result["confidence"] == 73.5


# ---------------------------------------------------------------------------
# 6. Warning generation for low confidence
# ---------------------------------------------------------------------------

class TestLowConfidenceWarning:
    """Verify warning when confidence < 50."""

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_low_confidence_warning_added(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="unclear",
            confidence=30,
            evidence_summary="limited evidence",
        )
        assert any("Low confidence RCA" in w for w in result["warnings"])

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_no_low_confidence_warning_at_50(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="db timeout",
            confidence=50,
            evidence_summary="sufficient evidence",
        )
        assert not any("Low confidence RCA" in w for w in result["warnings"])

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_no_low_confidence_warning_at_high_confidence(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="db timeout",
            confidence=95,
            evidence_summary="strong evidence",
        )
        assert not any("Low confidence RCA" in w for w in result["warnings"])

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_low_confidence_at_zero(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="unknown",
            confidence=0,
            evidence_summary="no evidence",
        )
        assert any("Low confidence RCA" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# 7. Warning generation for high-risk actions
# ---------------------------------------------------------------------------

class TestHighRiskWarning:
    """Verify warnings for high and critical risk levels."""

    @pytest.mark.parametrize(
        "incident_type",
        ["oomkill", "error_spike", "saturation", "cascading", "silent_failure"],
    )
    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_high_or_critical_risk_has_verify_warning(
        self, mock_yaml: MagicMock, mock_llm: MagicMock, incident_type: str
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type=incident_type,
            root_cause="test cause",
            confidence=80,
            evidence_summary="evidence",
        )
        assert any("VERIFY BEFORE ACTING" in w for w in result["warnings"])

    @pytest.mark.parametrize(
        "incident_type",
        ["timeout", "latency", "network", "missing_data", "flapping"],
    )
    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_medium_risk_no_verify_warning(
        self, mock_yaml: MagicMock, mock_llm: MagicMock, incident_type: str
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type=incident_type,
            root_cause="test cause",
            confidence=80,
            evidence_summary="evidence",
        )
        assert not any("VERIFY BEFORE ACTING" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# 8. verify_before_acting enforcement
# ---------------------------------------------------------------------------

class TestVerifyBeforeActing:
    """Verify that verify_before_acting is always True for high/critical risk."""

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_high_risk_forces_verify(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        templates = copy.deepcopy(REMEDIATION_TEMPLATES)
        # Force verify_before_acting to False in template
        templates["oomkill"]["verify_before_acting"] = False
        mock_yaml.return_value = templates

        result = generate_remediation(
            incident_type="oomkill",
            root_cause="memory leak",
            confidence=80,
            evidence_summary="evidence",
        )
        # Should be forced to True because risk_level is "high"
        assert result["verify_before_acting"] is True

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_critical_risk_forces_verify(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="cascading",
            root_cause="cascading failure",
            confidence=80,
            evidence_summary="evidence",
        )
        assert result["verify_before_acting"] is True

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_medium_risk_respects_template_verify(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="slow query",
            confidence=80,
            evidence_summary="evidence",
        )
        # timeout template has verify_before_acting=False, risk=medium
        assert result["verify_before_acting"] is False


# ---------------------------------------------------------------------------
# 9-10. Missing/Invalid YAML handling (covered in TestYAMLOverrideLoading)
# ---------------------------------------------------------------------------

# Already covered above. Adding a few more edge cases.

class TestYAMLEdgeCases:
    """Additional YAML edge cases."""

    def test_yaml_with_empty_overrides_key(self, tmp_path: Path):
        yaml_file = tmp_path / "override.yaml"
        yaml_file.write_text("overrides:\n")
        result = _load_yaml_overrides(yaml_path=yaml_file)
        # overrides: None is not a dict, so should fall back
        assert result == REMEDIATION_TEMPLATES

    def test_yaml_with_empty_dict_overrides(self, tmp_path: Path):
        yaml_file = tmp_path / "override.yaml"
        yaml_file.write_text("overrides: {}\n")
        result = _load_yaml_overrides(yaml_path=yaml_file)
        assert result == REMEDIATION_TEMPLATES

    def test_yaml_with_no_overrides_key(self, tmp_path: Path):
        yaml_file = tmp_path / "override.yaml"
        yaml_file.write_text("some_other_key: value\n")
        result = _load_yaml_overrides(yaml_path=yaml_file)
        # overrides defaults to {}, so no changes
        assert result == REMEDIATION_TEMPLATES


# ---------------------------------------------------------------------------
# 11. Template with ITSM context
# ---------------------------------------------------------------------------

class TestITSMContext:
    """Verify ITSM context flows through to LLM enrichment."""

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_itsm_rollback_plan_in_enriched_output(
        self,
        mock_yaml: MagicMock,
        mock_llm: MagicMock,
        mock_converse: MagicMock,
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        enriched = _make_enriched_json(
            immediate_actions=[
                "Rollback payment-svc to v2.2.0 per ServiceNow CHG001234",
                "Verify rollback via PR #847 revert",
            ]
        )
        mock_converse.return_value = _mock_converse_success(enriched)

        result = generate_remediation(
            incident_type="error_spike",
            root_cause="deployment regression",
            confidence=85,
            evidence_summary="Error rate 5x after deployment",
            itsm_context={
                "rollback_plan": "revert PR #847",
                "change_id": "CHG001234",
            },
        )
        assert any("ServiceNow" in a or "CHG001234" in a for a in result["immediate_actions"])

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_itsm_context_none_does_not_break(
        self,
        mock_yaml: MagicMock,
        mock_llm: MagicMock,
        mock_converse: MagicMock,
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        mock_converse.return_value = _mock_converse_success(_make_enriched_json())

        result = generate_remediation(
            incident_type="oomkill",
            root_cause="memory leak",
            confidence=80,
            evidence_summary="pod restarts",
            itsm_context=None,
        )
        assert result["source"] == "template+llm"


# ---------------------------------------------------------------------------
# 12. Template with DevOps context
# ---------------------------------------------------------------------------

class TestDevOpsContext:
    """Verify DevOps context flows through to LLM enrichment."""

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_devops_version_and_pr_in_enriched_output(
        self,
        mock_yaml: MagicMock,
        mock_llm: MagicMock,
        mock_converse: MagicMock,
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        enriched = _make_enriched_json(
            immediate_actions=[
                "Rollback deployment v2.3.1 (PR #847)",
                "Scale payment-svc to 8 replicas",
            ]
        )
        mock_converse.return_value = _mock_converse_success(enriched)

        result = generate_remediation(
            incident_type="oomkill",
            root_cause="memory leak in payment-svc",
            confidence=85,
            evidence_summary="Pod payment-svc-abc restarted 3 times",
            devops_context={"deployment_version": "v2.3.1", "pr_number": 847},
        )
        assert any("v2.3.1" in a for a in result["immediate_actions"])
        assert any("PR #847" in a for a in result["immediate_actions"])


# ---------------------------------------------------------------------------
# 13. Risk level validation
# ---------------------------------------------------------------------------

class TestRiskLevelValidation:
    """Verify risk levels are valid across all templates and outputs."""

    @pytest.mark.parametrize("incident_type", ALL_INCIDENT_TYPES)
    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_output_risk_level_is_valid(
        self, mock_yaml: MagicMock, mock_llm: MagicMock, incident_type: str
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type=incident_type,
            root_cause="test",
            confidence=80,
            evidence_summary="evidence",
        )
        assert result["risk_level"] in VALID_RISK_LEVELS

    def test_cascading_is_critical(self):
        assert REMEDIATION_TEMPLATES["cascading"]["risk_level"] == "critical"

    def test_timeout_is_medium(self):
        assert REMEDIATION_TEMPLATES["timeout"]["risk_level"] == "medium"


# ---------------------------------------------------------------------------
# 14. Confidence scoring
# ---------------------------------------------------------------------------

class TestConfidenceScoring:
    """Verify confidence value is correctly passed through and affects output."""

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_confidence_49_has_warning(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="unclear",
            confidence=49,
            evidence_summary="evidence",
        )
        assert result["confidence"] == 49
        assert any("Low confidence" in w for w in result["warnings"])

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_confidence_100_no_warning(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="confirmed db timeout",
            confidence=100,
            evidence_summary="strong evidence",
        )
        assert result["confidence"] == 100
        assert not any("Low confidence" in w for w in result["warnings"])


# ---------------------------------------------------------------------------
# 15. Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases: empty root cause, unknown incident type, etc."""

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_empty_root_cause(self, mock_yaml: MagicMock, mock_llm: MagicMock):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="",
            confidence=80,
            evidence_summary="evidence",
        )
        # Should still return a valid output
        assert REQUIRED_OUTPUT_KEYS.issubset(result.keys())
        assert result["source"] == "template_only"

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_unknown_incident_type_falls_back_to_error_spike(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="completely_unknown_type",
            root_cause="something broke",
            confidence=60,
            evidence_summary="evidence",
        )
        # Should fall back to error_spike template
        expected = REMEDIATION_TEMPLATES["error_spike"]
        assert result["immediate_actions"] == expected["immediate_actions"]
        assert result["permanent_fix"] == expected["permanent_fix"]
        assert result["risk_level"] == expected["risk_level"]

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_empty_evidence_summary(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="latency",
            root_cause="connection pool exhaustion",
            confidence=70,
            evidence_summary="",
        )
        assert REQUIRED_OUTPUT_KEYS.issubset(result.keys())

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_both_low_confidence_and_high_risk_generate_multiple_warnings(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="cascading",  # critical risk
            root_cause="unclear",
            confidence=20,  # low confidence
            evidence_summary="minimal evidence",
        )
        assert len(result["warnings"]) >= 2
        assert any("Low confidence" in w for w in result["warnings"])
        assert any("VERIFY BEFORE ACTING" in w for w in result["warnings"])

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_negative_confidence(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="timeout",
            root_cause="test",
            confidence=-10,
            evidence_summary="evidence",
        )
        assert result["confidence"] == -10
        # Negative is less than 50, so should get low confidence warning
        assert any("Low confidence" in w for w in result["warnings"])

    @patch("supervisor.remediation.converse")
    @patch("supervisor.remediation.llm_is_enabled", return_value=True)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_llm_failure_source_stays_template_only(
        self,
        mock_yaml: MagicMock,
        mock_llm: MagicMock,
        mock_converse: MagicMock,
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        mock_converse.return_value = _mock_converse_error()

        result = generate_remediation(
            incident_type="timeout",
            root_cause="test",
            confidence=80,
            evidence_summary="evidence",
        )
        # enrich_remediation_llm returns template (same object) on failure,
        # so source should be template_only
        assert result["source"] == "template_only"

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_runbook_hint_present_for_all_types(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        for incident_type in ALL_INCIDENT_TYPES:
            result = generate_remediation(
                incident_type=incident_type,
                root_cause="test",
                confidence=80,
                evidence_summary="evidence",
            )
            assert result["runbook_hint"], f"Missing runbook_hint for {incident_type}"

    @patch("supervisor.remediation.llm_is_enabled", return_value=False)
    @patch("supervisor.remediation._load_yaml_overrides")
    def test_medium_risk_no_forced_verify(
        self, mock_yaml: MagicMock, mock_llm: MagicMock
    ):
        mock_yaml.return_value = copy.deepcopy(REMEDIATION_TEMPLATES)
        result = generate_remediation(
            incident_type="network",  # medium risk, verify_before_acting=False
            root_cause="DNS failure",
            confidence=80,
            evidence_summary="evidence",
        )
        assert result["verify_before_acting"] is False
        assert result["risk_level"] == "medium"
