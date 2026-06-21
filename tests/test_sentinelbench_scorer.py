import pytest
from sentinelbench.scorer import RCAScorer, PASS_THRESHOLD
from sentinelbench.schema import ExpectedAnswer


def make_expected(**overrides):
    base = {
        "schema_version": "1.0",
        "root_cause_category": "connection_pool_exhaustion",
        "required_keywords": ["connection pool", "postgres"],
        "required_evidence_sources": ["splunk_logs", "dynatrace_apm"],
        "forbidden_keywords": ["network partition", "DNS"],
        "optimal_trajectory": [],
        "confidence_floor": 0.60,
    }
    base.update(overrides)
    return ExpectedAnswer(**base)


def make_result(**overrides):
    base = {
        "root_cause": "connection pool exhausted due to postgres slow queries",
        "summary": "connection_pool_exhaustion confirmed",
        "confidence": 75,
        "recommended_action": "investigate the connection pool settings",
        "playbook": [],
        "tools_called": [],
    }
    base.update(overrides)
    return base


SCORER = RCAScorer()


def test_root_cause_correctness_full_match():
    expected = make_expected()
    result = make_result(root_cause="connection_pool_exhaustion connection pool postgres max_connections")
    score = SCORER.score("s1", result, expected, ["splunk_logs", "dynatrace_apm"])
    assert score.root_cause_correctness == pytest.approx(1.0)


def test_root_cause_correctness_no_match():
    expected = make_expected()
    result = make_result(root_cause="nothing relevant here", summary="")
    score = SCORER.score("s1", result, expected, [])
    assert score.root_cause_correctness == pytest.approx(0.0)


def test_root_cause_correctness_category_only():
    expected = make_expected()
    result = make_result(root_cause="connection_pool_exhaustion but no keywords")
    score = SCORER.score("s1", result, expected, [])
    assert score.root_cause_correctness == pytest.approx(0.5)


def test_evidence_completeness_all_called():
    expected = make_expected()
    score = SCORER.score("s1", make_result(), expected, ["splunk_logs", "dynatrace_apm"])
    assert score.evidence_completeness == pytest.approx(1.0)


def test_evidence_completeness_half_called():
    expected = make_expected()
    score = SCORER.score("s1", make_result(), expected, ["splunk_logs"])
    assert score.evidence_completeness == pytest.approx(0.5)


def test_evidence_completeness_none_called():
    expected = make_expected()
    score = SCORER.score("s1", make_result(), expected, [])
    assert score.evidence_completeness == pytest.approx(0.0)


def test_red_herring_avoidance_no_forbidden():
    expected = make_expected(forbidden_keywords=["network partition"])
    result = make_result(root_cause="connection pool issue in postgres")
    score = SCORER.score("s1", result, expected, [])
    assert score.red_herring_avoidance == pytest.approx(1.0)


def test_red_herring_avoidance_forbidden_present():
    expected = make_expected(forbidden_keywords=["network partition"])
    result = make_result(root_cause="network partition caused the connection pool failure")
    score = SCORER.score("s1", result, expected, [])
    assert score.red_herring_avoidance == pytest.approx(0.0)


def test_composite_weighted_correctly():
    expected = make_expected(
        required_keywords=["connection pool", "postgres"],
        forbidden_keywords=[],
        optimal_trajectory=[],
        confidence_floor=0.75,
    )
    result = {
        "root_cause": "connection_pool_exhaustion connection pool postgres",
        "summary": "",
        "confidence": 75,
        "recommended_action": "restart the service",
        "playbook": [],
        "tools_called": [],
    }
    score = SCORER.score("s1", result, expected, ["splunk_logs", "dynatrace_apm"])
    expected_composite = (
        score.root_cause_correctness * 0.35
        + score.evidence_completeness * 0.20
        + score.tool_grounding * 0.15
        + score.red_herring_avoidance * 0.15
        + score.timeline_quality * 0.10
        + score.confidence_calibration * 0.03
        + score.action_quality * 0.02
    )
    assert score.composite == pytest.approx(expected_composite, abs=0.001)


def test_passed_true_when_composite_above_threshold():
    expected = make_expected(forbidden_keywords=[])
    result = make_result(
        root_cause="connection_pool_exhaustion connection pool postgres",
        recommended_action="investigate and restart",
    )
    score = SCORER.score("s1", result, expected, ["splunk_logs", "dynatrace_apm"])
    assert score.passed == (score.composite >= PASS_THRESHOLD)


def test_action_quality_full_score_with_keyword():
    expected = make_expected(forbidden_keywords=[])
    result = make_result(recommended_action="restart the payment service immediately")
    score = SCORER.score("s1", result, expected, [])
    assert score.action_quality == pytest.approx(1.0)


def test_action_quality_half_score_no_keyword():
    expected = make_expected(forbidden_keywords=[])
    result = make_result(recommended_action="fix the problem")
    score = SCORER.score("s1", result, expected, [])
    assert score.action_quality == pytest.approx(0.5)


def test_action_quality_zero_when_empty():
    expected = make_expected(forbidden_keywords=[])
    result = make_result(recommended_action="")
    score = SCORER.score("s1", result, expected, [])
    assert score.action_quality == pytest.approx(0.0)


def test_timeline_quality_neutral_when_no_trajectory():
    expected = make_expected(optimal_trajectory=[])
    score = SCORER.score("s1", make_result(), expected, [])
    assert score.timeline_quality == pytest.approx(0.8)


def test_confidence_calibration_decreases_when_far_from_floor():
    expected_near = make_expected(confidence_floor=0.75)
    expected_far = make_expected(confidence_floor=0.75)
    result_near = make_result(confidence=75)
    result_far = make_result(confidence=10)
    score_near = SCORER.score("s1", result_near, expected_near, [])
    score_far = SCORER.score("s1", result_far, expected_far, [])
    assert score_near.confidence_calibration > score_far.confidence_calibration
