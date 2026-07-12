"""R3 — Nightly Learning Pipeline.

One offline pass, in the mission's order:

  artifact audit → admission review (R1, with R2 benchmark signals)
  → memory quality (P2 usefulness) → benchmark agreement
  → investigation value metrics → corpus health (P3)
  → readiness gates (G1-G11) → trend reports (P4)

No runtime investigation is modified. Everything is derived from the
stores; every output is a versioned, deterministic report. Wave 3
remains disabled — this pipeline produces evidence, never flags.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from sentinel_core.intel_memory import MemoryStore
from sentinel_core.investigation_artifact import ArtifactStore
from sentinel_core.investigation_value.admission_executor import (
    run_admission_review,
)
from sentinel_core.investigation_value.benchmark_matcher import (
    run_benchmark_matching,
)
from sentinel_core.investigation_value.corpus_health import (
    corpus_health_report,
)
from sentinel_core.investigation_value.effectiveness import (
    learning_effectiveness_report,
)
from sentinel_core.investigation_value.readiness import GateInputs
from sentinel_core.investigation_value.shadow_pipeline import (
    run_readiness_evaluation,
)
from sentinel_core.investigation_value.usefulness import (
    corpus_usefulness_report,
)

NIGHTLY_SCHEMA_VERSION = 1


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True, indent=2))
    tmp.replace(path)


def run_nightly_learning(
    artifact_root: Path | str,
    memory_root: Path | str,
    output_dir: Path | str,
    generated_at: str,
    scenarios: Mapping[str, Any] | None = None,
    extra_signals: Mapping[str, Mapping[str, Any]] | None = None,
    extra_gate_inputs: GateInputs | None = None,
) -> dict[str, Any]:
    """Full nightly pass. Deterministic given stores + inputs + timestamp.

    ``scenarios``: SentinelBench corpus (``load_all_scenarios()``); when
    None the benchmark stage is skipped (recorded as such — silence is
    never success). ``extra_signals``: operator/replay signals merged
    over the matcher's benchmark signals per artifact_id.
    """
    astore = ArtifactStore(artifact_root)
    out = Path(output_dir)

    # ── 1. Artifact audit (counts before any action)
    audit_before = {
        s: len(astore.list_ids(s))
        for s in ("candidate", "admitted", "quarantined", "rejected")
    }

    # ── 2. Benchmark matching (R2) over candidates + admitted
    bench_signals: dict[str, dict[str, Any]] = {}
    bench_note = "skipped: no scenario corpus supplied"
    if scenarios:
        artifacts = {}
        for state in ("candidate", "admitted"):
            for aid in astore.list_ids(state):
                try:
                    artifacts[aid] = astore.load(aid)
                except Exception:
                    continue
        bench_signals = run_benchmark_matching(artifacts, scenarios)
        bench_note = f"matched {len(bench_signals)}/{len(artifacts)}"

    # merge operator/replay signals on top of matcher output
    signals: dict[str, dict[str, Any]] = {
        k: dict(v) for k, v in bench_signals.items()
    }
    for aid, sig in (extra_signals or {}).items():
        signals.setdefault(str(aid), {}).update(dict(sig))

    # ── 3. Admission review (R1)
    admission = run_admission_review(
        artifact_root, memory_root, at=generated_at,
        signals_by_artifact=signals,
    )

    # ── 4. Memory quality (P2) over the ACTIVE corpus
    mstore = MemoryStore(memory_root)
    records = [mstore.load(mid) for mid in mstore.list_ids()]
    usefulness = corpus_usefulness_report(records)
    _write_json(out / "memory_usefulness.json", usefulness)

    # ── 5. Benchmark agreement aggregates (G4 inputs)
    agreements = sorted(
        v["benchmark_agreement"] for v in bench_signals.values()
    )
    bench_mean = (round(sum(agreements) / len(agreements), 4)
                   if agreements else None)
    bench_min = agreements[0] if agreements else None

    # ── 6. Corpus health (P3)
    health = corpus_health_report(records, as_of=generated_at)
    _write_json(out / "corpus_health.json", health)

    # ── 7. Readiness gates (G1-G11) — reuses the shadow pipeline
    base = extra_gate_inputs or GateInputs()
    gate_inputs = GateInputs(
        demotion_rate_30d=base.demotion_rate_30d,
        replay_agreement_rate=base.replay_agreement_rate,
        replay_unexplained_regressions=base.replay_unexplained_regressions,
        bench_matched_mean=(base.bench_matched_mean
                             if base.bench_matched_mean is not None
                             else bench_mean),
        bench_matched_min=(base.bench_matched_min
                            if base.bench_matched_min is not None
                            else bench_min),
        similarity_same_cause_mean=base.similarity_same_cause_mean,
        similarity_diff_cause_mean=base.similarity_diff_cause_mean,
        mean_iip=base.mean_iip,
        mean_pgs=base.mean_pgs,
        regression_share=base.regression_share,
        false_retrieval_rate=base.false_retrieval_rate,
        max_calibration_bin_error=base.max_calibration_bin_error,
        p99_latency_delta=base.p99_latency_delta,
        failsafe_drill_completed=base.failsafe_drill_completed,
    )
    readiness = run_readiness_evaluation(
        artifact_root, memory_root, out, generated_at,
        extra_inputs=gate_inputs,
    )

    # ── 8. Trend reports (P4)
    effectiveness = learning_effectiveness_report(
        records,
        usefulness_series=[usefulness["mean_usefulness"]]
        if usefulness["record_count"] else [],
    )
    _write_json(out / "learning_effectiveness.json", effectiveness)

    summary = {
        "schema_version": NIGHTLY_SCHEMA_VERSION,
        "generated_at": str(generated_at),
        "artifact_audit_before": audit_before,
        "benchmark_matching": bench_note,
        "admission": {k: admission[k] for k in
                       ("admitted", "quarantined", "rejected",
                        "demoted", "validated", "errors")},
        "memory": {"active_records": len(records),
                    "mean_usefulness": usefulness["mean_usefulness"]},
        "corpus_health": {"duplicates": health["duplicates"]["count"],
                           "conflicts": len(health["conflicts"]),
                           "stale": health["stale"]["count"]},
        "readiness": {
            "passed": readiness["readiness"]["passed_count"],
            "failed": readiness["readiness"]["failed_count"],
            "verdict": readiness["readiness"]["verdict"],
            "wave3_enabled": readiness["readiness"]["wave3_enabled"],
        },
        "learning": {"is_learning": effectiveness["is_learning"],
                      "improving": effectiveness["improving"],
                      "degrading": effectiveness["degrading"]},
    }
    _write_json(out / "nightly_summary.json", summary)
    return summary


__all__ = ["NIGHTLY_SCHEMA_VERSION", "run_nightly_learning"]
