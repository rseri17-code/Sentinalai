"""Shadow evaluation pipeline — Wave 3 Readiness Program, WS6.

Offline, produce-only. Reads the artifact store and memory store,
computes whatever gate inputs are currently measurable, evaluates
G1-G11, writes ``wave3_readiness.json`` (deterministic JSON) and
appends one line to ``readiness_history.jsonl`` (append-only trend).

No runtime reads. No planner influence. No retrieval. Timestamps are
caller-supplied (never ``now()`` here — platform determinism rule).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sentinel_core.intel_memory import MemoryStore
from sentinel_core.investigation_artifact import ArtifactStore
from sentinel_core.investigation_value.metrics import METRICS_SCHEMA_VERSION
from sentinel_core.investigation_value.readiness import (
    GateInputs,
    evaluate_gates,
)

PIPELINE_SCHEMA_VERSION = 1


def _corpus_stats(memory_root: Path | str) -> tuple[int, dict[str, int]]:
    """Admitted-corpus size total + per incident_type."""
    store = MemoryStore(memory_root)
    per_class: dict[str, int] = {}
    total = 0
    for mid in store.list_ids():
        try:
            rec = store.load(mid)
        except Exception:
            continue
        total += 1
        key = rec.incident_type or "(unclassified)"
        per_class[key] = per_class.get(key, 0) + 1
    return total, per_class


def _admission_stats(artifact_root: Path | str) -> dict[str, Any]:
    """Counts by state directory + demotion rate from the audit trail."""
    store = ArtifactStore(artifact_root)
    counts = {s: len(store.list_ids(s))
              for s in ("candidate", "admitted", "quarantined", "rejected")}
    demotions = 0
    admissions = 0
    drill = False
    for event in store.audit_events():
        to = str(event.get("to", ""))
        frm = str(event.get("from", ""))
        if to == "admitted" or to == "decision:admitted":
            admissions += 1
        if frm == "admitted" and to in ("quarantined", "rejected"):
            demotions += 1
            drill = True   # a demotion has been exercised end-to-end
    rate = (demotions / admissions) if admissions else None
    return {"counts": counts, "demotion_rate": rate,
            "drill_completed": drill if (demotions or admissions) else None}


def run_readiness_evaluation(
    artifact_root: Path | str,
    memory_root: Path | str,
    output_dir: Path | str,
    generated_at: str,
    extra_inputs: GateInputs | None = None,
) -> dict[str, Any]:
    """One pipeline pass: corpus stats → gate inputs → G1-G11 → reports.

    ``extra_inputs`` supplies measurements this pipeline cannot derive
    from the stores alone (replay/bench agreement, similarity precision,
    shadow-window metrics, latency delta). Fields left None simply FAIL
    their gates with "insufficient data" — fail-closed.

    Writes ``{output_dir}/wave3_readiness.json`` (atomic, sort_keys) and
    appends to ``{output_dir}/readiness_history.jsonl`` (append-only).
    Returns the full report dict.
    """
    total, per_class = _corpus_stats(memory_root)
    admission = _admission_stats(artifact_root)

    base = extra_inputs or GateInputs()
    inputs = GateInputs(
        admitted_total=total,
        admitted_per_class=per_class,
        demotion_rate_30d=(
            base.demotion_rate_30d if base.demotion_rate_30d is not None
            else admission["demotion_rate"]
        ),
        replay_agreement_rate=base.replay_agreement_rate,
        replay_unexplained_regressions=base.replay_unexplained_regressions,
        bench_matched_mean=base.bench_matched_mean,
        bench_matched_min=base.bench_matched_min,
        similarity_same_cause_mean=base.similarity_same_cause_mean,
        similarity_diff_cause_mean=base.similarity_diff_cause_mean,
        mean_iip=base.mean_iip,
        mean_pgs=base.mean_pgs,
        regression_share=base.regression_share,
        false_retrieval_rate=base.false_retrieval_rate,
        max_calibration_bin_error=base.max_calibration_bin_error,
        p99_latency_delta=base.p99_latency_delta,
        failsafe_drill_completed=(
            base.failsafe_drill_completed
            if base.failsafe_drill_completed is not None
            else admission["drill_completed"]
        ),
    )

    gate_report = evaluate_gates(inputs)
    report: dict[str, Any] = {
        "schema_version": PIPELINE_SCHEMA_VERSION,
        "metrics_schema_version": METRICS_SCHEMA_VERSION,
        "generated_at": str(generated_at),
        "corpus": {
            "admitted_total": total,
            "admitted_per_class": {k: per_class[k]
                                    for k in sorted(per_class)},
            "artifact_states": admission["counts"],
        },
        "readiness": gate_report,
    }

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(report, sort_keys=True, indent=2)
    target = out / "wave3_readiness.json"
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(payload)
    tmp.replace(target)

    # Append-only historical trend — one compact line per evaluation.
    trend_line = json.dumps({
        "generated_at": str(generated_at),
        "admitted_total": total,
        "passed_count": gate_report["passed_count"],
        "failed_count": gate_report["failed_count"],
        "all_passed": gate_report["all_passed"],
        "blocking_gates": gate_report["blocking_gates"],
    }, sort_keys=True)
    with open(out / "readiness_history.jsonl", "a") as f:
        f.write(trend_line + "\n")

    return report


__all__ = ["PIPELINE_SCHEMA_VERSION", "run_readiness_evaluation"]
