import pytest
from sentinelbench.red_herring import RedHerringSpec, RedHerringInjector
from sentinelbench.scorer import RCAScorer
from sentinelbench.schema import ExpectedAnswer


SPEC = RedHerringSpec(
    noise_service="cdn-edge-proxy",
    noise_keywords=["network packet loss", "CDN latency spike"],
    noise_metric="network_packet_loss_percent",
    noise_value=18.5,
)


def make_expected(**overrides):
    base = {
        "schema_version": "1.0",
        "root_cause_category": "connection_pool_exhaustion",
        "required_keywords": ["connection pool"],
        "required_evidence_sources": ["splunk_logs"],
        "forbidden_keywords": ["network packet loss", "CDN"],
    }
    base.update(overrides)
    return ExpectedAnswer(**base)


def test_inject_adds_red_herring_noise_key():
    injector = RedHerringInjector()
    evidence = {"splunk_logs": {"message": "connection pool exhausted"}}
    result = injector.inject(evidence, SPEC)
    assert "_red_herring_noise" in result


def test_inject_preserves_original_keys():
    injector = RedHerringInjector()
    evidence = {"splunk_logs": {"msg": "ok"}, "metrics": [1, 2]}
    result = injector.inject(evidence, SPEC)
    assert "splunk_logs" in result
    assert "metrics" in result
    assert result["splunk_logs"] == {"msg": "ok"}


def test_inject_does_not_mutate_original():
    injector = RedHerringInjector()
    evidence = {"splunk_logs": {"msg": "ok"}}
    original_keys = set(evidence.keys())
    injector.inject(evidence, SPEC)
    assert set(evidence.keys()) == original_keys


def test_extract_noise_keywords_includes_noise_service():
    injector = RedHerringInjector()
    keywords = injector.extract_noise_keywords(SPEC)
    assert SPEC.noise_service in keywords


def test_extract_noise_keywords_includes_noise_metric():
    injector = RedHerringInjector()
    keywords = injector.extract_noise_keywords(SPEC)
    assert SPEC.noise_metric in keywords


def test_scorer_red_herring_avoidance_zero_when_noise_in_rca():
    expected = make_expected()
    scorer = RCAScorer()
    result = {
        "root_cause": "network packet loss caused the payment service timeout — connection pool impacted",
        "summary": "",
        "confidence": 70,
        "recommended_action": "investigate",
        "playbook": [],
        "tools_called": [],
    }
    score = scorer.score("s1", result, expected, [])
    assert score.red_herring_avoidance == pytest.approx(0.0)


def test_scorer_red_herring_avoidance_one_when_noise_absent():
    expected = make_expected()
    scorer = RCAScorer()
    result = {
        "root_cause": "connection pool exhaustion due to postgres lock contention",
        "summary": "",
        "confidence": 70,
        "recommended_action": "investigate",
        "playbook": [],
        "tools_called": [],
    }
    score = scorer.score("s1", result, expected, [])
    assert score.red_herring_avoidance == pytest.approx(1.0)
