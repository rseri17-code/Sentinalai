"""Gold Standard Investigation Dataset + IQS — produce-only evaluator tests.

Coverage: immutable benchmark record (compose + deterministic id, human +
postmortem blocks), per-record metrics (all ten), decisive-evidence latency,
localization short-name handling, IQS from validated metrics only + coverage,
NOT_MEASURED without labels, determinism.
"""
from __future__ import annotations

import json

from sentinel_core.investigation_value.gold_standard import (
    evaluate_dataset,
    gold_record,
    record_metrics,
)
from sentinel_core.investigation_value.scientific_validation import NOT_MEASURED


def _result(rc="db pool exhaustion", loc="db", decisive=None):
    return {
        "root_cause": rc, "confidence": 80, "incident_id": "INC-1",
        "incident_type": "saturation",
        "_hypothesis_graph": {"hypotheses": [
            {"name": rc, "status": "confirmed", "confidence": 80,
             "supporting_evidence": [{"key": "oom_logs"}, {"key": "mem_metrics"}],
             "refuting_evidence": []},
            {"name": "bad deploy", "status": "ruled_out", "confidence": 40,
             "supporting_evidence": [],
             "refuting_evidence": [{"key": "deploy_check"}]}]},
        "_elimination_narrative": {"winner": rc, "survived_disconfirmation": True,
                                    "ruled_out": [{"name": "bad deploy",
                                                    "reason": "refuted"}]},
        "_counterfactual": "heap dump would change this",
        "_evidence_snapshot": {"oom_logs": True, "mem_metrics": True,
                                "deploy_check": True, "trace_x": True},
        "_investigation_validation": {
            "root_cause_verification": {"verification_status": "supports"},
            "evidence_validation": {"evidence_validation_score": 0.85},
            "confidence_reconstruction": {"raw_confidence": 82,
                                           "evidence_confidence": 78},
            "investigation_completeness": {
                "investigation_completeness_score": 0.8},
            "counterfactual": {"counterfactual_residual_score": 0.5}},
        "_decision_intelligence": {"evidence_attribution": {
            "decisive_evidence": decisive if decisive is not None
            else ["oom_logs"],
            "importance_ranking": ["oom_logs", "mem_metrics", "deploy_check",
                                    "trace_x"]}},
        "_causal_investigation": {
            "localization": {"root_cause_service": loc}},
    }


def _incident():
    return {"incident_id": "INC-1", "incident_type": "saturation",
            "service": "db"}


def _pm():
    return {"root_cause": "db pool exhaustion",
            "root_cause_keywords": ["db", "pool", "exhaustion"],
            "resolution_time_ms": 3600000, "outcome": "resolved"}


def _human():
    return {"validated_root_cause": "db pool exhaustion",
            "interventions": ["scaled pool"], "agreed": True,
            "operator_confidence": 90}


def _gold(seq=("oom_logs", "mem_metrics", "deploy_check", "trace_x"),
          human=None, postmortem=None, decisive=None, replay_hash="rh1"):
    return gold_record(_result(decisive=decisive), _incident(),
                       evidence_sequence=list(seq), human=human,
                       postmortem=postmortem, commit="a0d55c6", model="opus",
                       replay_hash=replay_hash)


# ---------------------------------------------------------------------------
# Gold record
# ---------------------------------------------------------------------------

class TestGoldRecord:
    def test_captures_investigation(self):
        g = _gold(postmortem=_pm(), human=_human())
        assert g["authoritative"]["root_cause"] == "db pool exhaustion"
        assert len(g["hypotheses"]) == 2
        assert g["eliminated"][0]["name"] == "bad deploy"
        assert g["localization"] == "db"
        assert g["evidence_attribution"]["decisive"] == ["oom_logs"]
        assert g["operator"]["present"] is True
        assert g["postmortem"]["present"] is True
        assert g["evidence_sequence_is_true_order"] is True

    def test_deterministic_id(self):
        assert gold_record(_result(), _incident())["record_id"] == \
            gold_record(_result(), _incident())["record_id"]

    def test_no_operator_no_postmortem(self):
        g = gold_record(_result(), _incident())
        assert g["operator"]["present"] is False
        assert g["postmortem"]["present"] is False

    def test_json_safe(self):
        g = _gold(postmortem=_pm())
        assert g == json.loads(json.dumps(g))


# ---------------------------------------------------------------------------
# Per-record metrics
# ---------------------------------------------------------------------------

class TestRecordMetrics:
    def test_all_metrics_present(self):
        m = record_metrics(_gold(postmortem=_pm(), human=_human()))
        for k in ("hypothesis_efficiency", "evidence_efficiency",
                   "unnecessary_evidence_avoided", "decisive_evidence_latency",
                   "false_lead_avoidance", "localization_accuracy",
                   "confidence_calibration", "investigation_completeness",
                   "operator_agreement", "replay_fidelity"):
            assert k in m

    def test_decisive_latency_earliest_is_best(self):
        # decisive is oom_logs, first in sequence -> latency score 1.0
        m = record_metrics(_gold(postmortem=_pm(), decisive=["oom_logs"]))
        assert m["decisive_evidence_latency"] == 1.0

    def test_decisive_latency_late_is_worse(self):
        m = record_metrics(_gold(postmortem=_pm(), decisive=["trace_x"]))
        assert m["decisive_evidence_latency"] < 1.0

    def test_localization_short_name(self):
        m = record_metrics(_gold(postmortem=_pm()))
        assert m["localization_accuracy"] == 1.0     # "db" in "db pool..."

    def test_localization_wrong(self):
        g = gold_record(_result(loc="frontend"), _incident(), postmortem=_pm())
        assert record_metrics(g)["localization_accuracy"] == 0.0

    def test_operator_agreement(self):
        m = record_metrics(_gold(human=_human()))
        assert m["operator_agreement"] == 1.0

    def test_unlabeled_metrics_none(self):
        m = record_metrics(gold_record(_result(), _incident()))
        assert m["localization_accuracy"] is None
        assert m["operator_agreement"] is None
        assert m["confidence_calibration"] is None


# ---------------------------------------------------------------------------
# Dataset evaluation + IQS
# ---------------------------------------------------------------------------

class TestEvaluateDataset:
    def test_iqs_full_coverage_when_labeled(self):
        recs = [_gold(postmortem=_pm(), human=_human()) for _ in range(3)]
        ev = evaluate_dataset(recs)
        assert ev["iqs_coverage"] == 1.0
        assert isinstance(ev["investigation_quality_score"], float)
        assert 0.0 <= ev["investigation_quality_score"] <= 1.0

    def test_iqs_lower_coverage_unlabeled(self):
        recs = [gold_record(_result(), _incident()) for _ in range(3)]
        ev = evaluate_dataset(recs)
        assert ev["iqs_coverage"] < 1.0        # labeled metrics missing

    def test_metrics_carry_sample_size(self):
        recs = [_gold(postmortem=_pm()) for _ in range(3)]
        ev = evaluate_dataset(recs)
        assert ev["metrics"]["localization_accuracy"]["n"] == 3
        assert "limitations" in ev["metrics"]["localization_accuracy"]

    def test_empty_dataset(self):
        ev = evaluate_dataset([])
        assert ev["n_records"] == 0
        assert ev["investigation_quality_score"] == NOT_MEASURED

    def test_deterministic_and_json_safe(self):
        recs = [_gold(postmortem=_pm(), human=_human()) for _ in range(3)]
        a = evaluate_dataset(recs)
        b = evaluate_dataset(recs)
        assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)
        assert a == json.loads(json.dumps(a))
