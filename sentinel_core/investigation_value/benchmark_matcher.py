"""R2 — Deterministic benchmark association.

Associates each Investigation Artifact with the closest SentinelBench
scenario. The benchmark is EVIDENCE, never authority: the association
is a signal fed to admission (satisfies Q4, may raise Q7) and to gate
G4 — it never modifies the artifact (immutable) and never influences a
running investigation.

Match score (closed-form, deterministic):
    0.6 · token-jaccard(root_cause, scenario.expected_root_cause)
  + 0.2 · [incident_type == scenario.incident_input.incident_type]
  + 0.2 · [service == scenario.incident_input.service]
Associations below MATCH_THRESHOLD are discarded (no forced matches).

Agreement score: the matched scenario re-scored through the existing
``score_investigation`` with an investigation_output derived from the
artifact — SentinelBench's own arithmetic, unchanged.
"""
from __future__ import annotations

from typing import Any, Mapping

from sentinel_core.investigation_value.metrics import _jaccard, _tokens

MATCHER_SCHEMA_VERSION = 1
MATCH_THRESHOLD = 0.30
# Benchmark disagreement (admission Q7 / audit) below this overall.
DISAGREEMENT_THRESHOLD = 0.50


def match_scenario(
    artifact: Any, scenarios: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Best deterministic association, or None below threshold.

    ``scenarios``: mapping scenario_id → Scenario (the shape returned by
    ``tests.synthetic.runner.load_all_scenarios``). Ties break on
    scenario_id ascending (canonical-sort discipline).
    """
    rc_tokens = _tokens(str(getattr(artifact, "root_cause", "")))
    a_type = str(getattr(artifact, "incident_type", "") or "")
    a_svc = str(getattr(artifact, "service", "") or "")

    best: tuple[float, str] | None = None
    for sid in sorted(scenarios):
        sc = scenarios[sid]
        rc_overlap = _jaccard(rc_tokens, _tokens(sc.expected_root_cause))
        # Evidence, never authority: identity alone (service + type) is
        # not an association — the root cause itself must overlap.
        if rc_overlap == 0.0:
            continue
        score = 0.6 * rc_overlap
        inc = sc.incident_input or {}
        if a_type and a_type == str(inc.get("incident_type", "")):
            score += 0.2
        if a_svc and a_svc == str(inc.get("service", "")):
            score += 0.2
        score = round(score, 4)
        if score >= MATCH_THRESHOLD and (best is None or score > best[0]):
            best = (score, sid)

    if best is None:
        return None
    return {"scenario_id": best[1], "match_score": best[0]}


def agreement_score(artifact: Any, scenario: Any) -> float:
    """SentinelBench overall for the artifact against its matched
    scenario — bench arithmetic reused verbatim (evidence, not
    authority)."""
    from tests.synthetic.scoring import score_investigation

    evidence = getattr(artifact, "evidence_key_summary", {}) or {}
    io = {
        "root_cause": str(getattr(artifact, "root_cause", "")),
        "confidence": int(getattr(artifact, "confidence", 0) or 0),
        "evidence_keys": list(evidence.get("keys") or ()),
        "decision_signals": [],
        "mtti_ms": sum(
            int(p.get("elapsed_ms", 0) or 0)
            for p in (getattr(artifact, "worker_execution_summary", {})
                       or {}).values()
            if isinstance(p, dict)
        ),
        "runtime_cost": int(getattr(artifact, "runtime_cost", 0) or 0),
    }
    return float(score_investigation(scenario, io).overall_score)


def run_benchmark_matching(
    artifacts: Mapping[str, Any], scenarios: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    """Match every artifact; return admission-ready signals keyed by
    artifact_id: benchmark_pointer, match_score, benchmark_agreement,
    benchmark_disagreement (Q7 signal when agreement < threshold)."""
    signals: dict[str, dict[str, Any]] = {}
    for aid in sorted(artifacts):
        artifact = artifacts[aid]
        m = match_scenario(artifact, scenarios)
        if m is None:
            continue
        agree = round(agreement_score(artifact, scenarios[m["scenario_id"]]), 4)
        signals[aid] = {
            "benchmark_pointer": f"bench:{m['scenario_id']}",
            "benchmark_match_score": m["match_score"],
            "benchmark_agreement": agree,
            "benchmark_disagreement": agree < DISAGREEMENT_THRESHOLD,
        }
    return signals


__all__ = [
    "DISAGREEMENT_THRESHOLD",
    "MATCHER_SCHEMA_VERSION",
    "MATCH_THRESHOLD",
    "agreement_score",
    "match_scenario",
    "run_benchmark_matching",
]
