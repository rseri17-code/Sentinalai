"""Tests for severity detection and investigation budget scaling."""


from supervisor.severity import (
    normalize_moogsoft_severity,
    normalize_itsm_tier,
    detect_severity,
    get_budget_for_severity,
)
from supervisor.guardrails import ExecutionBudget


# =========================================================================
# normalize_moogsoft_severity — integer inputs
# =========================================================================


class TestNormalizeMoogsoftSeverityInts:
    def test_severity_1(self):
        assert normalize_moogsoft_severity(1) == 1

    def test_severity_2(self):
        assert normalize_moogsoft_severity(2) == 2

    def test_severity_3(self):
        assert normalize_moogsoft_severity(3) == 3

    def test_severity_4(self):
        assert normalize_moogsoft_severity(4) == 4

    def test_severity_5(self):
        assert normalize_moogsoft_severity(5) == 5

    def test_severity_0_out_of_range(self):
        """Severity 0 is out of range 1-5 and should default to 3."""
        assert normalize_moogsoft_severity(0) == 3

    def test_severity_99_out_of_range(self):
        """Severity 99 is out of range 1-5 and should default to 3."""
        assert normalize_moogsoft_severity(99) == 3

    def test_severity_negative(self):
        """Negative severity should default to 3."""
        assert normalize_moogsoft_severity(-1) == 3


# =========================================================================
# normalize_moogsoft_severity — string inputs
# =========================================================================


class TestNormalizeMoogsoftSeverityStrings:
    def test_critical(self):
        assert normalize_moogsoft_severity("critical") == 1

    def test_major(self):
        assert normalize_moogsoft_severity("major") == 2

    def test_warning(self):
        assert normalize_moogsoft_severity("warning") == 3

    def test_minor(self):
        assert normalize_moogsoft_severity("minor") == 4

    def test_info(self):
        assert normalize_moogsoft_severity("info") == 5

    def test_case_insensitive_upper(self):
        """CRITICAL should normalize to level 1."""
        assert normalize_moogsoft_severity("CRITICAL") == 1

    def test_case_insensitive_title(self):
        """Critical should normalize to level 1."""
        assert normalize_moogsoft_severity("Critical") == 1

    def test_case_insensitive_with_whitespace(self):
        """ ' critical ' with surrounding whitespace should normalize to 1."""
        assert normalize_moogsoft_severity(" critical ") == 1

    def test_major_uppercase(self):
        assert normalize_moogsoft_severity("MAJOR") == 2

    def test_warning_mixed_case(self):
        assert normalize_moogsoft_severity("Warning") == 3

    def test_minor_uppercase(self):
        assert normalize_moogsoft_severity("MINOR") == 4

    def test_info_title_case(self):
        assert normalize_moogsoft_severity("Info") == 5

    def test_integer_string(self):
        """A string like '2' should parse as integer severity."""
        assert normalize_moogsoft_severity("2") == 2

    def test_integer_string_out_of_range(self):
        """A string like '99' should default to 3."""
        assert normalize_moogsoft_severity("99") == 3

    def test_unrecognized_string(self):
        """An unrecognized string should default to 3."""
        assert normalize_moogsoft_severity("unknown") == 3

    def test_empty_string(self):
        """Empty string should default to 3."""
        assert normalize_moogsoft_severity("") == 3


# =========================================================================
# normalize_moogsoft_severity — None and other types
# =========================================================================


class TestNormalizeMoogsoftSeverityEdgeCases:
    def test_none_returns_default(self):
        assert normalize_moogsoft_severity(None) == 3

    def test_float_returns_default(self):
        """Float type is not handled; should default to 3."""
        assert normalize_moogsoft_severity(2.5) == 3

    def test_list_returns_default(self):
        """List type should default to 3."""
        assert normalize_moogsoft_severity([1]) == 3


# =========================================================================
# normalize_itsm_tier
# =========================================================================


class TestNormalizeItsmTier:
    def test_tier_dash_1(self):
        assert normalize_itsm_tier("tier-1") == 1

    def test_tier_dash_2(self):
        assert normalize_itsm_tier("tier-2") == 2

    def test_tier_dash_3(self):
        assert normalize_itsm_tier("tier-3") == 3

    def test_tier_dash_4(self):
        assert normalize_itsm_tier("tier-4") == 4

    def test_tier_space_1(self):
        """'Tier 1' format should work."""
        assert normalize_itsm_tier("Tier 1") == 1

    def test_tier_space_2(self):
        assert normalize_itsm_tier("Tier 2") == 2

    def test_bare_int_1(self):
        """Bare integer 1 should return 1."""
        assert normalize_itsm_tier(1) == 1

    def test_bare_int_4(self):
        assert normalize_itsm_tier(4) == 4

    def test_bare_int_string(self):
        """String '1' should return 1."""
        assert normalize_itsm_tier("1") == 1

    def test_critical_string(self):
        """'critical' maps to tier 1."""
        assert normalize_itsm_tier("critical") == 1

    def test_none_returns_none(self):
        assert normalize_itsm_tier(None) is None

    def test_unknown_string_returns_none(self):
        assert normalize_itsm_tier("unknown") is None

    def test_empty_string_returns_none(self):
        assert normalize_itsm_tier("") is None

    def test_out_of_range_int_returns_none(self):
        """Integer 5 is not a valid tier (1-4), should return None."""
        assert normalize_itsm_tier(5) is None

    def test_out_of_range_int_zero(self):
        assert normalize_itsm_tier(0) is None

    def test_tier_case_insensitive(self):
        assert normalize_itsm_tier("TIER-1") == 1

    def test_tier_whitespace(self):
        assert normalize_itsm_tier(" tier-2 ") == 2


# =========================================================================
# detect_severity — Moogsoft only (no ITSM context)
# =========================================================================


class TestDetectSeverityMoogsoftOnly:
    def test_moogsoft_critical(self):
        sev = detect_severity({"severity": 1})
        assert sev.level == 1
        assert sev.label == "critical"
        assert sev.source == "moogsoft"

    def test_moogsoft_major(self):
        sev = detect_severity({"severity": 2})
        assert sev.level == 2
        assert sev.label == "high"

    def test_moogsoft_warning(self):
        sev = detect_severity({"severity": 3})
        assert sev.level == 3
        assert sev.label == "medium"

    def test_moogsoft_minor(self):
        sev = detect_severity({"severity": 4})
        assert sev.level == 4
        assert sev.label == "low"

    def test_moogsoft_info(self):
        sev = detect_severity({"severity": 5})
        assert sev.level == 5
        assert sev.label == "info"

    def test_moogsoft_string_critical(self):
        sev = detect_severity({"severity": "critical"})
        assert sev.level == 1
        assert sev.label == "critical"

    def test_no_severity_field_defaults_to_medium(self):
        """When incident dict has no 'severity' key, default to level 3."""
        sev = detect_severity({})
        assert sev.level == 3
        assert sev.label == "medium"

    def test_empty_incident_defaults_to_medium(self):
        sev = detect_severity({})
        assert sev.level == 3


# =========================================================================
# detect_severity — highest severity wins (composite logic)
# =========================================================================


class TestDetectSeverityComposite:
    def test_moogsoft_sev3_itsm_tier1_wins_critical(self):
        """Moogsoft sev-3 + ITSM tier-1 -> level 1 (tier-1 always critical)."""
        sev = detect_severity(
            {"severity": 3},
            {"ci": {"tier": "tier-1"}},
        )
        assert sev.level == 1
        assert sev.label == "critical"

    def test_moogsoft_sev1_itsm_tier3_stays_critical(self):
        """Moogsoft sev-1 + ITSM tier-3 -> level 1 (Moogsoft says critical)."""
        sev = detect_severity(
            {"severity": 1},
            {"ci": {"tier": "tier-3"}},
        )
        assert sev.level == 1
        assert sev.label == "critical"

    def test_moogsoft_sev5_itsm_tier2_escalates_to_high(self):
        """Moogsoft sev-5 + ITSM tier-2 -> level 2 (tier-2 caps at high)."""
        sev = detect_severity(
            {"severity": 5},
            {"ci": {"tier": "tier-2"}},
        )
        assert sev.level == 2
        assert sev.label == "high"

    def test_moogsoft_sev1_itsm_tier2_stays_critical(self):
        """Moogsoft sev-1 + ITSM tier-2 -> level 1 (Moogsoft already more severe)."""
        sev = detect_severity(
            {"severity": 1},
            {"ci": {"tier": "tier-2"}},
        )
        assert sev.level == 1

    def test_moogsoft_sev4_itsm_tier4_no_change(self):
        """Tier-4 never downgrades. Moogsoft sev-4 stays at level 4."""
        sev = detect_severity(
            {"severity": 4},
            {"ci": {"tier": "tier-4"}},
        )
        assert sev.level == 4
        assert sev.label == "low"

    def test_moogsoft_sev3_itsm_tier3_no_change(self):
        """Tier-3 does not modify level."""
        sev = detect_severity(
            {"severity": 3},
            {"ci": {"tier": "tier-3"}},
        )
        assert sev.level == 3

    def test_both_critical(self):
        """Both sources critical -> level 1, composite source."""
        sev = detect_severity(
            {"severity": 1},
            {"ci": {"tier": "tier-1"}},
        )
        assert sev.level == 1
        assert sev.source == "composite"

    def test_source_is_itsm_when_itsm_escalates(self):
        """When ITSM tier-1 escalates, source should reflect ITSM."""
        sev = detect_severity(
            {"severity": 4},
            {"ci": {"tier": "tier-1"}},
        )
        assert sev.level == 1
        assert sev.source == "itsm"


# =========================================================================
# detect_severity — no ITSM context
# =========================================================================


class TestDetectSeverityNoItsmContext:
    def test_none_itsm_context(self):
        sev = detect_severity({"severity": 2}, None)
        assert sev.level == 2
        assert sev.source == "moogsoft"

    def test_itsm_context_with_no_ci(self):
        """ITSM context dict exists but has no 'ci' key."""
        sev = detect_severity({"severity": 3}, {"other": "data"})
        assert sev.level == 3

    def test_itsm_context_with_no_tier(self):
        """CI dict exists but has no 'tier' key."""
        sev = detect_severity({"severity": 4}, {"ci": {"name": "svc"}})
        assert sev.level == 4


# =========================================================================
# detect_severity — neither source available
# =========================================================================


class TestDetectSeverityNoSourceData:
    def test_empty_incident_no_itsm(self):
        """Neither Moogsoft severity nor ITSM context -> default level 3."""
        sev = detect_severity({}, None)
        assert sev.level == 3
        assert sev.label == "medium"

    def test_empty_incident_empty_itsm(self):
        sev = detect_severity({}, {})
        assert sev.level == 3


# =========================================================================
# Budget scaling per severity level
# =========================================================================


class TestBudgetScaling:
    def test_level_1_budget(self):
        sev = detect_severity({"severity": 1})
        assert sev.budget == 35

    def test_level_2_budget(self):
        sev = detect_severity({"severity": 2})
        assert sev.budget == 30

    def test_level_3_budget(self):
        sev = detect_severity({"severity": 3})
        assert sev.budget == 25

    def test_level_4_budget(self):
        sev = detect_severity({"severity": 4})
        assert sev.budget == 20

    def test_level_5_budget(self):
        sev = detect_severity({"severity": 5})
        assert sev.budget == 15


# =========================================================================
# Deep dive enabled/disabled per level
# =========================================================================


class TestDeepDive:
    def test_deep_dive_enabled_level_1(self):
        sev = detect_severity({"severity": 1})
        assert sev.deep_dive_enabled is True

    def test_deep_dive_enabled_level_2(self):
        sev = detect_severity({"severity": 2})
        assert sev.deep_dive_enabled is True

    def test_deep_dive_enabled_level_3(self):
        sev = detect_severity({"severity": 3})
        assert sev.deep_dive_enabled is True

    def test_deep_dive_disabled_level_4(self):
        sev = detect_severity({"severity": 4})
        assert sev.deep_dive_enabled is False

    def test_deep_dive_disabled_level_5(self):
        sev = detect_severity({"severity": 5})
        assert sev.deep_dive_enabled is False


# =========================================================================
# Deep dive bonus per level
# =========================================================================


class TestDeepDiveBonus:
    def test_bonus_level_1(self):
        sev = detect_severity({"severity": 1})
        assert sev.deep_dive_bonus == 15

    def test_bonus_level_2(self):
        sev = detect_severity({"severity": 2})
        assert sev.deep_dive_bonus == 10

    def test_bonus_level_3(self):
        sev = detect_severity({"severity": 3})
        assert sev.deep_dive_bonus == 10

    def test_bonus_level_4(self):
        sev = detect_severity({"severity": 4})
        assert sev.deep_dive_bonus == 0

    def test_bonus_level_5(self):
        sev = detect_severity({"severity": 5})
        assert sev.deep_dive_bonus == 0


# =========================================================================
# LLM reasoning forced for critical/high
# =========================================================================


class TestLlmReasoning:
    def test_llm_reasoning_on_critical(self):
        sev = detect_severity({"severity": 1})
        assert sev.llm_reasoning is True

    def test_llm_reasoning_on_high(self):
        sev = detect_severity({"severity": 2})
        assert sev.llm_reasoning is True

    def test_llm_reasoning_off_medium(self):
        sev = detect_severity({"severity": 3})
        assert sev.llm_reasoning is False

    def test_llm_reasoning_off_low(self):
        sev = detect_severity({"severity": 4})
        assert sev.llm_reasoning is False

    def test_llm_reasoning_off_info(self):
        sev = detect_severity({"severity": 5})
        assert sev.llm_reasoning is False


# =========================================================================
# get_budget_for_severity
# =========================================================================


class TestGetBudgetForSeverity:
    def test_returns_execution_budget(self):
        sev = detect_severity({"severity": 1})
        budget = get_budget_for_severity(sev)
        assert isinstance(budget, ExecutionBudget)

    def test_critical_budget_max_calls(self):
        sev = detect_severity({"severity": 1})
        budget = get_budget_for_severity(sev)
        assert budget.max_calls == 35

    def test_high_budget_max_calls(self):
        sev = detect_severity({"severity": 2})
        budget = get_budget_for_severity(sev)
        assert budget.max_calls == 30

    def test_medium_budget_max_calls(self):
        sev = detect_severity({"severity": 3})
        budget = get_budget_for_severity(sev)
        assert budget.max_calls == 25

    def test_low_budget_max_calls(self):
        sev = detect_severity({"severity": 4})
        budget = get_budget_for_severity(sev)
        assert budget.max_calls == 20

    def test_info_budget_max_calls(self):
        sev = detect_severity({"severity": 5})
        budget = get_budget_for_severity(sev)
        assert budget.max_calls == 15

    def test_budget_starts_with_zero_calls_made(self):
        sev = detect_severity({"severity": 1})
        budget = get_budget_for_severity(sev)
        assert budget.calls_made == 0

    def test_budget_can_call_initially(self):
        sev = detect_severity({"severity": 5})
        budget = get_budget_for_severity(sev)
        assert budget.can_call()

    def test_budget_remaining_matches_max(self):
        sev = detect_severity({"severity": 2})
        budget = get_budget_for_severity(sev)
        assert budget.remaining() == 30


# =========================================================================
# Edge cases
# =========================================================================


class TestEdgeCases:
    def test_severity_zero(self):
        """Severity 0 is out of range; should default to level 3."""
        sev = detect_severity({"severity": 0})
        assert sev.level == 3

    def test_severity_99(self):
        """Severity 99 is out of range; should default to level 3."""
        sev = detect_severity({"severity": 99})
        assert sev.level == 3

    def test_tier_unknown(self):
        """Unknown tier string should not affect severity."""
        sev = detect_severity({"severity": 4}, {"ci": {"tier": "unknown"}})
        assert sev.level == 4

    def test_empty_itsm_dict(self):
        sev = detect_severity({"severity": 2}, {})
        assert sev.level == 2

    def test_itsm_ci_is_none(self):
        sev = detect_severity({"severity": 3}, {"ci": None})
        assert sev.level == 3

    def test_severity_none_value(self):
        """Explicit None severity should default to medium."""
        sev = detect_severity({"severity": None})
        assert sev.level == 3

    def test_tier_out_of_range_5(self):
        """Tier 5 is not valid (1-4), should be treated as None."""
        sev = detect_severity({"severity": 2}, {"ci": {"tier": 5}})
        assert sev.level == 2

    def test_investigation_severity_dataclass_fields(self):
        """Verify all expected fields exist on InvestigationSeverity."""
        sev = detect_severity({"severity": 1})
        assert hasattr(sev, "level")
        assert hasattr(sev, "label")
        assert hasattr(sev, "source")
        assert hasattr(sev, "budget")
        assert hasattr(sev, "deep_dive_enabled")
        assert hasattr(sev, "deep_dive_bonus")
        assert hasattr(sev, "llm_reasoning")
