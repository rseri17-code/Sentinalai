"""EIC adapter — convert a SentinelAI investigation result into a NEUTRAL
Enterprise Investigation Challenge submission.

This is the ONLY EIC file coupled to SentinelAI. The benchmark itself
(eic/benchmark.py) never imports this — engine agnosticism means the scorer
grades a plain Submission dict, and each engine supplies its own adapter at
the boundary. This one lets SentinelAI *compete* in the EIC; other engines
(a human, Dynatrace, a research agent) provide their own equivalent mapping.

Produce-only, deterministic. Reads existing shadow outputs; recomputes
nothing; changes no runtime path.
"""
from __future__ import annotations

from typing import Any, Mapping

from sentinel_core.eic.benchmark import make_submission


def sentinelai_submission(result: Mapping[str, Any],
                          *, task_id: str, engine_version: str = "",
                          evidence_sequence: list[str] | None = None,
                          replay_hash: str = "") -> dict[str, Any]:
    """Map a completed SentinelAI result to a neutral EIC submission.

    Composition only — pulls the authoritative RCA/confidence plus, when the
    shadow stack is enabled, the hypothesis/localization/decisive-evidence
    signals. Absent shadow metadata simply yields empty neutral fields, so a
    baseline (shadow-off) SentinelAI still produces a valid submission.
    """
    graph = result.get("_hypothesis_graph") or {}
    narr = result.get("_elimination_narrative") or {}
    di = result.get("_decision_intelligence") or {}
    causal = result.get("_causal_investigation") or {}

    hyps = [str(h.get("name", "")) for h in graph.get("hypotheses", [])
            if isinstance(h, dict)] if isinstance(graph, dict) else []
    ruled_out = [str(x.get("name", "")) for x in (narr.get("ruled_out", []) or [])
                 if isinstance(x, dict)] if isinstance(narr, dict) else []
    decisive = (di.get("evidence_attribution", {}) or {}).get(
        "decisive_evidence", []) if isinstance(di, dict) else []
    localized = (causal.get("localization", {}) or {}).get(
        "root_cause_service", "") if isinstance(causal, dict) else ""

    if evidence_sequence is None:
        snap = result.get("_evidence_snapshot") or {}
        evidence_sequence = sorted(
            k for k, v in snap.items()
            if v and not str(k).startswith("_")) if isinstance(snap, dict) else []

    return make_submission(
        engine="sentinelai",
        engine_version=engine_version,
        task_id=task_id,
        root_cause=str(result.get("root_cause", "")),
        localized_service=str(localized),
        hypotheses=hyps,
        ruled_out=ruled_out,
        evidence_used=list(evidence_sequence),
        decisive_evidence=list(decisive),
        confidence=int(result.get("confidence", 0) or 0),
        proof=str(result.get("reasoning", "")),
        replay_hash=replay_hash,
    )


__all__ = ["sentinelai_submission"]
