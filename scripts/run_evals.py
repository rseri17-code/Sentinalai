#!/usr/bin/env python3
"""SentinalAI deep eval runner.

Runs all 10 expected incident scenarios through the supervisor,
computes quality scores per eval dimension via both rule-based and
LLM-as-judge scoring, and emits OTEL metrics to Splunk dashboards.

Usage:
    # With OTEL collector running:
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 python scripts/run_evals.py

    # With LLM-as-judge (Bedrock):
    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
    AWS_REGION=us-east-1 \
    python scripts/run_evals.py --llm-judge

    # Dry-run (no OTEL, no Bedrock, prints results to stdout):
    python scripts/run_evals.py
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

# Ensure project root is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from supervisor.agent import SentinalAISupervisor
from supervisor.eval_metrics import record_eval_score
from supervisor.llm_judge import judge_and_record
from tests.fixtures.expected_rca_outputs import EXPECTED_RCA


def _build_mock_workers(sup: SentinalAISupervisor, incident_id: str):
    """Wire mock workers for a given incident (reuse test infra)."""
    from tests.test_supervisor import _build_mock_workers as _build
    _build(sup, incident_id)


def _score_keywords(result: dict, expected: dict) -> float:
    """Score: fraction of expected keywords found in root_cause."""
    root_cause = result.get("root_cause", "").lower()
    keywords = expected.get("root_cause_keywords", [])
    if not keywords:
        return 1.0
    matched = sum(1 for kw in keywords if kw.lower() in root_cause)
    return matched / len(keywords)


def _score_confidence_range(result: dict, expected: dict) -> float:
    """Score: 1.0 if confidence is in expected range, 0.0 otherwise."""
    conf = result.get("confidence", 0)
    lo = expected.get("confidence_min", 0)
    hi = expected.get("confidence_max", 100)
    if lo <= conf <= hi:
        return 1.0
    # Partial credit: how close?
    if conf < lo:
        return max(0.0, 1.0 - (lo - conf) / 100)
    return max(0.0, 1.0 - (conf - hi) / 100)


def _score_reasoning_quality(result: dict, expected: dict) -> float:
    """Score: fraction of reasoning requirements met."""
    reasoning = result.get("reasoning", "").lower()
    reqs = expected.get("reasoning_requirements", {})
    if not reqs:
        return 1.0

    checks_passed = 0
    checks_total = 0

    for key, value in reqs.items():
        if key == "must_explain_causality" and value:
            checks_total += 1
            if any(w in reasoning for w in ["caused", "led to", "resulted", "because", "due to"]):
                checks_passed += 1
        elif key == "must_mention_timeline" and value:
            checks_total += 1
            if any(w in reasoning for w in ["before", "after", "preceded", "followed", "timeline"]):
                checks_passed += 1
        elif key == "must_identify_first_fault" and value:
            checks_total += 1
            if any(w in reasoning for w in ["first", "initial", "origin", "root"]):
                checks_passed += 1
        elif key == "keywords_required":
            for kw in value:
                checks_total += 1
                if kw.lower() in reasoning:
                    checks_passed += 1
        elif key == "must_explain_pattern" and isinstance(value, str):
            checks_total += 1
            if any(w in reasoning for w in value.lower().split()):
                checks_passed += 1
        elif key == "must_mention_oomkill" and value:
            checks_total += 1
            if "oom" in reasoning or "kill" in reasoning or "memory" in reasoning:
                checks_passed += 1
        elif key == "must_correlate_deployment" and value:
            checks_total += 1
            if "deploy" in reasoning:
                checks_passed += 1
        elif key == "must_mention_error_type" and value:
            checks_total += 1
            if any(w in reasoning for w in ["exception", "error", "null"]):
                checks_passed += 1
        elif key == "must_explain_cascade" and value:
            checks_total += 1
            if any(w in reasoning for w in ["cascade", "propagat", "downstream", "spread"]):
                checks_passed += 1
        elif key == "must_identify_root_trigger" and value:
            checks_total += 1
            if any(w in reasoning for w in ["root", "origin", "trigger", "initial"]):
                checks_passed += 1
        elif key == "must_acknowledge_limited_data" and value:
            checks_total += 1
            if any(w in reasoning for w in ["limited", "missing", "unavailable", "insufficient"]):
                checks_passed += 1
        elif key == "must_identify_pattern" and value:
            checks_total += 1
            if any(w in reasoning for w in ["pattern", "recurring", "intermittent", "cycle"]):
                checks_passed += 1
        elif key == "must_explain_intermittent_nature" and value:
            checks_total += 1
            if any(w in reasoning for w in ["intermittent", "sporadic", "recurring", "periodic"]):
                checks_passed += 1
        elif key == "must_explain_indirect_cause" and value:
            checks_total += 1
            if any(w in reasoning for w in ["indirect", "upstream", "pipeline", "stale"]):
                checks_passed += 1
        elif key == "must_identify_upstream_failure" and value:
            checks_total += 1
            if any(w in reasoning for w in ["upstream", "pipeline", "source"]):
                checks_passed += 1
        elif key == "must_correlate_config_change" and value:
            checks_total += 1
            if "config" in reasoning:
                checks_passed += 1
        elif key == "must_explain_cpu_spike" and value:
            checks_total += 1
            if "cpu" in reasoning:
                checks_passed += 1
        elif key == "must_identify_infrastructure_cause" and value:
            checks_total += 1
            if any(w in reasoning for w in ["dns", "infrastructure", "network"]):
                checks_passed += 1
        elif key == "must_explain_broad_impact" and value:
            checks_total += 1
            if any(w in reasoning for w in ["multiple", "broad", "services", "widespread"]):
                checks_passed += 1
        elif key == "must_mention_backend" and value:
            checks_total += 1
            if any(w in reasoning for w in ["backend", "elasticsearch", "database"]):
                checks_passed += 1

    return checks_passed / checks_total if checks_total > 0 else 1.0


def _score_timeline(result: dict, expected: dict) -> float:
    """Score: 1.0 if timeline has entries, 0.5 if empty, factored by length."""
    timeline = result.get("evidence_timeline", [])
    if not timeline:
        return 0.0
    # More entries = better (up to a point)
    return min(1.0, len(timeline) / 3)


INCIDENT_TYPES = {
    "INC12345": "timeout", "INC12346": "oomkill", "INC12347": "error_spike",
    "INC12348": "latency", "INC12349": "saturation", "INC12350": "network",
    "INC12351": "cascading", "INC12352": "missing_data", "INC12353": "flapping",
    "INC12354": "silent_failure",
}


def run_eval(incident_id: str, use_llm_judge: bool = False) -> dict:
    """Run a single eval scenario and return scores."""
    expected = EXPECTED_RCA[incident_id]
    sup = SentinalAISupervisor()
    _build_mock_workers(sup, incident_id)

    start = time.monotonic()
    result = sup.investigate(incident_id)
    elapsed_ms = (time.monotonic() - start) * 1000

    incident_type = INCIDENT_TYPES.get(incident_id, "unknown")

    # Rule-based scores (always computed)
    keyword_score = _score_keywords(result, expected)
    confidence_score = _score_confidence_range(result, expected)
    reasoning_score = _score_reasoning_quality(result, expected)
    timeline_score = _score_timeline(result, expected)
    overall = (keyword_score + confidence_score + reasoning_score + timeline_score) / 4

    rule_scores = {
        "root_cause_accuracy": round(keyword_score, 3),
        "confidence_calibration": round(confidence_score, 3),
        "reasoning_quality": round(reasoning_score, 3),
        "timeline_completeness": round(timeline_score, 3),
        "overall": round(overall, 3),
    }

    # Emit rule-based scores as OTEL metrics
    for dimension, score in rule_scores.items():
        record_eval_score(incident_id, incident_type, f"rule_based.{dimension}", score)

    # LLM-as-judge deep eval (Bedrock) — emits its own OTEL metrics
    judge_scores = None
    if use_llm_judge:
        judge_scores = judge_and_record(incident_id, incident_type, expected, result)

    output = {
        "incident_id": incident_id,
        "incident_type": incident_type,
        "root_cause": result.get("root_cause", ""),
        "confidence": result.get("confidence", 0),
        "elapsed_ms": round(elapsed_ms, 1),
        "scores": rule_scores,
    }
    if judge_scores:
        output["llm_judge_scores"] = judge_scores

    return output


def main():
    use_llm_judge = "--llm-judge" in sys.argv

    print("=" * 72)
    print("SentinalAI Deep Eval Runner")
    print(f"  LLM-as-judge: {'ENABLED (Bedrock)' if use_llm_judge else 'disabled (rule-based only)'}")
    print("=" * 72)

    results = []
    for incident_id in sorted(EXPECTED_RCA.keys()):
        print(f"\n--- {incident_id} ---")
        try:
            r = run_eval(incident_id, use_llm_judge=use_llm_judge)
            results.append(r)
            print(f"  Root cause: {r['root_cause'][:80]}")
            print(f"  Confidence: {r['confidence']}")
            print(f"  Duration:   {r['elapsed_ms']}ms")
            print(f"  Rule scores:  {json.dumps(r['scores'], indent=None)}")
            if r.get("llm_judge_scores"):
                print(f"  Judge scores: {json.dumps(r['llm_judge_scores'], indent=None)}")
        except Exception as e:
            print(f"  ERROR: {e}")
            results.append({"incident_id": incident_id, "error": str(e)})

    # Summary
    scored = [r for r in results if "scores" in r]
    if scored:
        avg_overall = sum(r["scores"]["overall"] for r in scored) / len(scored)
        avg_accuracy = sum(r["scores"]["root_cause_accuracy"] for r in scored) / len(scored)
        avg_confidence = sum(r["scores"]["confidence_calibration"] for r in scored) / len(scored)
        avg_reasoning = sum(r["scores"]["reasoning_quality"] for r in scored) / len(scored)
        avg_timeline = sum(r["scores"]["timeline_completeness"] for r in scored) / len(scored)

        print("\n" + "=" * 72)
        print("AGGREGATE EVAL SCORES (rule-based)")
        print("=" * 72)
        print(f"  Scenarios run:          {len(scored)}/{len(EXPECTED_RCA)}")
        print(f"  Root cause accuracy:    {avg_accuracy:.1%}")
        print(f"  Confidence calibration: {avg_confidence:.1%}")
        print(f"  Reasoning quality:      {avg_reasoning:.1%}")
        print(f"  Timeline completeness:  {avg_timeline:.1%}")
        print(f"  Overall:                {avg_overall:.1%}")

    # LLM-as-judge summary
    judged = [r for r in results if r.get("llm_judge_scores")]
    if judged:
        print("\n" + "-" * 72)
        print("AGGREGATE EVAL SCORES (LLM-as-judge)")
        print("-" * 72)
        for dim in ["root_cause_accuracy", "causal_reasoning", "evidence_usage",
                     "timeline_quality", "actionability", "overall"]:
            vals = [r["llm_judge_scores"][dim] for r in judged if dim in r["llm_judge_scores"]]
            if vals:
                print(f"  {dim:30s} {sum(vals)/len(vals):.1%}")

    print("=" * 72)

    # Flush metrics if OTEL is configured
    try:
        from opentelemetry import metrics as otel_metrics
        provider = otel_metrics.get_meter_provider()
        if hasattr(provider, "force_flush"):
            provider.force_flush()
            print("\nOTEL metrics flushed to collector.")
    except Exception:
        pass


if __name__ == "__main__":
    main()
