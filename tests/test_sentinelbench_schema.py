import pytest
from sentinelbench.schema import Scenario, ExpectedAnswer, RCAScore, ScoreCard


def make_scenario(**overrides):
    base = {
        "schema_version": "1.0",
        "scenario_id": "test-001",
        "title": "Test Scenario",
        "failure_mode": "timeout",
        "severity": "p1",
        "affected_service": "test-service",
        "difficulty": "medium",
        "available_evidence": ["splunk_logs", "metrics"],
    }
    base.update(overrides)
    return base


def make_expected(**overrides):
    base = {
        "schema_version": "1.0",
        "root_cause_category": "connection_pool_exhaustion",
        "required_keywords": ["connection pool", "postgres"],
        "required_evidence_sources": ["splunk_logs"],
    }
    base.update(overrides)
    return base


def test_scenario_parses_correctly():
    s = Scenario(**make_scenario())
    assert s.scenario_id == "test-001"
    assert s.severity == "p1"
    assert "splunk_logs" in s.available_evidence


def test_scenario_default_tags_empty():
    s = Scenario(**make_scenario())
    assert s.tags == []


def test_scenario_tags_parsed():
    s = Scenario(**make_scenario(tags=["database", "timeout"]))
    assert s.tags == ["database", "timeout"]


def test_expected_answer_parses_correctly():
    ea = ExpectedAnswer(**make_expected())
    assert ea.root_cause_category == "connection_pool_exhaustion"
    assert ea.max_investigation_loops == 15
    assert ea.confidence_floor == 0.60


def test_expected_answer_defaults():
    ea = ExpectedAnswer(**make_expected())
    assert ea.forbidden_keywords == []
    assert ea.optimal_trajectory == []


def test_model_validator_rejects_overlapping_keywords():
    with pytest.raises(ValueError, match="overlap"):
        ExpectedAnswer(**make_expected(
            required_keywords=["connection pool", "postgres"],
            forbidden_keywords=["postgres", "DNS"],
        ))


def test_model_validator_allows_disjoint_keywords():
    ea = ExpectedAnswer(**make_expected(
        required_keywords=["connection pool"],
        forbidden_keywords=["DNS", "network"],
    ))
    assert ea.required_keywords == ["connection pool"]


def test_rca_score_composite_in_range():
    score = RCAScore(
        scenario_id="test-001",
        root_cause_correctness=0.8,
        evidence_completeness=0.9,
        tool_grounding=0.7,
        red_herring_avoidance=1.0,
        timeline_quality=0.6,
        confidence_calibration=0.5,
        action_quality=1.0,
        composite=0.82,
        passed=True,
    )
    assert 0.0 <= score.composite <= 1.0


def test_rca_score_passed_true():
    score = RCAScore(
        scenario_id="test-001",
        root_cause_correctness=1.0,
        evidence_completeness=1.0,
        tool_grounding=1.0,
        red_herring_avoidance=1.0,
        timeline_quality=1.0,
        confidence_calibration=1.0,
        action_quality=1.0,
        composite=0.75,
        passed=True,
    )
    assert score.passed is True


def test_rca_score_defaults_empty_details():
    score = RCAScore(
        scenario_id="test-001",
        root_cause_correctness=0.5,
        evidence_completeness=0.5,
        tool_grounding=0.5,
        red_herring_avoidance=1.0,
        timeline_quality=0.8,
        confidence_calibration=0.5,
        action_quality=0.5,
        composite=0.55,
        passed=False,
    )
    assert score.details == {}


def test_scorecard_pass_rate_equals_passed_over_total():
    from sentinelbench.schema import RCAScore
    scores = [
        RCAScore(
            scenario_id=f"s{i}",
            root_cause_correctness=0.8,
            evidence_completeness=0.8,
            tool_grounding=0.8,
            red_herring_avoidance=1.0,
            timeline_quality=0.8,
            confidence_calibration=0.8,
            action_quality=1.0,
            composite=0.82,
            passed=(i < 3),
        )
        for i in range(5)
    ]
    card = ScoreCard(
        run_id="abc123",
        timestamp="2024-06-21T00:00:00Z",
        total_scenarios=5,
        passed=3,
        failed=2,
        pass_rate=3 / 5,
        mean_composite=0.82,
        scores=scores,
    )
    assert card.pass_rate == pytest.approx(0.6)
    assert card.passed + card.failed == card.total_scenarios
