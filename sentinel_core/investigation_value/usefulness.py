"""P2 — Investigation Intelligence: per-record memory usefulness.

Evaluates memory by investigation value, not storage quality. Every
component answers one question: *would this field have made a future
investigation better?* A field that never changes future investigations
scores 0 and must never influence retrieval.

Eight deterministic components, each ∈ [0, 1]:

  root_cause    recurrence of this cause in the corpus (a cause seen
                 again is a cause worth recalling)
  evidence      how often this record's evidence keys recur corpus-wide
                 (transferable acquisition guidance)
  worker        runtime-cost signal present (enables cost priors)
  planner       real planner path captured (teaches sequencing)
  hypothesis    alternatives context: false leads or decision snapshot
                 present (teaches ranking, not just the answer)
  confidence    calibratable: confidence + quality score both present
  remediation   resolution captured (MTTR recall)
  false_lead    anti-pattern signal captured (teaches elimination)

usefulness = mean(components). Pure, offline, reproducible.
"""
from __future__ import annotations

from typing import Any, Iterable

from sentinel_core.investigation_value.metrics import _jaccard, _tokens

USEFULNESS_SCHEMA_VERSION = 1

# A cause must recur in ≥ this many OTHER records (token-jaccard ≥ 0.5)
# to reach full root-cause usefulness.
_RECURRENCE_SATURATION = 3


def record_usefulness(record: Any, corpus: Iterable[Any]) -> dict[str, Any]:
    """Usefulness report for one MemoryRecord against its corpus."""
    others = [r for r in corpus
              if getattr(r, "memory_id", None) != record.memory_id]

    my_cause = _tokens(record.detected_root_cause)
    cause_recurrence = sum(
        1 for r in others
        if my_cause and _jaccard(my_cause,
                                  _tokens(r.detected_root_cause)) >= 0.5
    )
    my_evidence = set(record.evidence_collected)
    if my_evidence and others:
        evidence_recurrence = sum(
            1 for r in others if my_evidence & set(r.evidence_collected)
        ) / len(others)
    else:
        evidence_recurrence = 0.0

    components = {
        "root_cause": round(min(1.0,
                                 cause_recurrence / _RECURRENCE_SATURATION), 4),
        "evidence": round(evidence_recurrence, 4),
        "worker": 1.0 if record.runtime_cost > 0 else 0.0,
        "planner": 1.0 if record.planner_decisions else 0.0,
        "hypothesis": 1.0 if (record.false_leads
                               or record.decision_trace.get(
                                   "decision_summary")) else 0.0,
        "confidence": 1.0 if (record.confidence > 0
                               and record.investigation_score > 0) else 0.0,
        "remediation": 1.0 if record.resolution else 0.0,
        "false_lead": 1.0 if record.false_leads else 0.0,
    }
    usefulness = round(sum(components.values()) / len(components), 4)
    return {
        "memory_id": record.memory_id,
        "usefulness": usefulness,
        "components": components,
    }


def corpus_usefulness_report(corpus: Iterable[Any]) -> dict[str, Any]:
    """Per-record usefulness + corpus aggregates, sorted deterministically
    (usefulness DESC, memory_id ASC)."""
    records = sorted(corpus, key=lambda r: r.memory_id)
    rows = [record_usefulness(r, records) for r in records]
    rows.sort(key=lambda x: (-x["usefulness"], x["memory_id"]))
    n = len(rows)
    mean = round(sum(x["usefulness"] for x in rows) / n, 4) if n else 0.0
    component_means = {}
    if n:
        for key in rows[0]["components"]:
            component_means[key] = round(
                sum(x["components"][key] for x in rows) / n, 4)
    return {
        "schema_version": USEFULNESS_SCHEMA_VERSION,
        "record_count": n,
        "mean_usefulness": mean,
        "component_means": component_means,
        "records": rows,
        # Retrieval-influence rule: components with corpus-wide mean 0
        # demonstrably never change investigations — listed explicitly.
        "never_influences_retrieval": sorted(
            k for k, v in component_means.items() if v == 0.0),
    }


__all__ = ["USEFULNESS_SCHEMA_VERSION", "corpus_usefulness_report",
           "record_usefulness"]
