"""Wave 3 readiness gates G1-G11 — automatic, objective evaluation.

Every gate is a pure predicate over caller-supplied measurements. A
measurement that is unavailable (None) FAILS its gate with the blocking
reason "insufficient data" — fail-closed, never fail-open.

Wave 3 remains DISABLED regardless of the score: the report's
``wave3_enabled`` field is hard-coded False. This module produces
evidence; it never flips flags.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

READINESS_SCHEMA_VERSION = 1

# Thresholds from the Investigation Value Audit, Phase 5.
MIN_CORPUS_TOTAL          = 500
MIN_CORPUS_PER_CLASS      = 20
MAX_DEMOTION_RATE         = 0.05
MIN_REPLAY_AGREEMENT      = 0.95
MIN_BENCH_MATCHED_MEAN    = 0.75
MIN_BENCH_MATCHED_FLOOR   = 0.50
MIN_SIMILARITY_SEPARATION = 0.25
MAX_DIFF_CAUSE_MEAN       = 0.45
MIN_MEAN_IIP              = 0.40
MIN_MEAN_PGS              = 0.50
MAX_REGRESSION_SHARE      = 0.05
MAX_FALSE_RETRIEVAL_RATE  = 0.10
MAX_CALIBRATION_BIN_ERROR = 0.15
MAX_P99_LATENCY_DELTA     = 0.05


@dataclass(frozen=True)
class GateInputs:
    """Measurements consumed by the gates. None = not yet measurable."""
    admitted_total:            int | None = None
    admitted_per_class:        Mapping[str, int] = field(default_factory=dict)
    demotion_rate_30d:         float | None = None
    replay_agreement_rate:     float | None = None
    replay_unexplained_regressions: int | None = None
    bench_matched_mean:        float | None = None
    bench_matched_min:         float | None = None
    similarity_same_cause_mean: float | None = None
    similarity_diff_cause_mean: float | None = None
    mean_iip:                  float | None = None
    mean_pgs:                  float | None = None
    regression_share:          float | None = None
    false_retrieval_rate:      float | None = None
    max_calibration_bin_error: float | None = None
    p99_latency_delta:         float | None = None
    failsafe_drill_completed:  bool | None = None


def _gate(name: str, description: str, value: Any, threshold: str,
          passed: bool | None, next_action: str) -> dict[str, Any]:
    """One gate row. ``passed is None`` ⇒ insufficient data ⇒ FAIL."""
    if passed is None:
        return {
            "gate": name, "description": description,
            "value": value, "threshold": threshold,
            "passed": False,
            "blocking_reason": "insufficient data — measurement unavailable",
            "next_action": next_action,
        }
    return {
        "gate": name, "description": description,
        "value": value, "threshold": threshold,
        "passed": bool(passed),
        "blocking_reason": "" if passed else "threshold not met",
        "next_action": "" if passed else next_action,
    }


def evaluate_gates(inputs: GateInputs) -> dict[str, Any]:
    """Evaluate G1-G11. Deterministic; same inputs → identical report."""
    i = inputs
    per_class = dict(i.admitted_per_class or {})
    classes_ready = sorted(
        c for c, n in per_class.items() if n >= MIN_CORPUS_PER_CLASS
    )

    g1_value = {"admitted_total": i.admitted_total,
                "classes_at_min_depth": classes_ready,
                "per_class": {k: per_class[k] for k in sorted(per_class)}}
    g1_pass = None if i.admitted_total is None else (
        i.admitted_total >= MIN_CORPUS_TOTAL and bool(classes_ready)
    )

    g5_pass: bool | None = None
    g5_value: Any = None
    if i.similarity_same_cause_mean is not None \
            and i.similarity_diff_cause_mean is not None:
        sep = i.similarity_same_cause_mean - i.similarity_diff_cause_mean
        g5_value = {"same_cause_mean": i.similarity_same_cause_mean,
                    "diff_cause_mean": i.similarity_diff_cause_mean,
                    "separation": round(sep, 4)}
        g5_pass = sep >= MIN_SIMILARITY_SEPARATION \
            and i.similarity_diff_cause_mean <= MAX_DIFF_CAUSE_MEAN

    g6_pass: bool | None = None
    if i.mean_iip is not None and i.mean_pgs is not None:
        g6_pass = i.mean_iip >= MIN_MEAN_IIP and i.mean_pgs >= MIN_MEAN_PGS

    g3_pass: bool | None = None
    if i.replay_agreement_rate is not None \
            and i.replay_unexplained_regressions is not None:
        g3_pass = i.replay_agreement_rate >= MIN_REPLAY_AGREEMENT \
            and i.replay_unexplained_regressions == 0

    g4_pass: bool | None = None
    if i.bench_matched_mean is not None and i.bench_matched_min is not None:
        g4_pass = i.bench_matched_mean >= MIN_BENCH_MATCHED_MEAN \
            and i.bench_matched_min >= MIN_BENCH_MATCHED_FLOOR

    gates = [
        _gate("G1", "minimum corpus size (total + per-class depth)",
              g1_value,
              f">= {MIN_CORPUS_TOTAL} total AND >= {MIN_CORPUS_PER_CLASS} "
              "in at least one incident_type",
              g1_pass,
              "enable produce-only flags in staging; accumulate corpus"),
        _gate("G2", "admission precision (retroactive demotions / 30d)",
              i.demotion_rate_30d, f"<= {MAX_DEMOTION_RATE}",
              None if i.demotion_rate_30d is None
              else i.demotion_rate_30d <= MAX_DEMOTION_RATE,
              "run nightly re-evaluation; wire Q5-Q7 signals"),
        _gate("G3", "replay agreement on sampled admitted records",
              {"agreement": i.replay_agreement_rate,
               "unexplained_regressions": i.replay_unexplained_regressions},
              f">= {MIN_REPLAY_AGREEMENT} and 0 unexplained regressions",
              g3_pass,
              "run SentinelReplay over admitted-record scenarios"),
        _gate("G4", "benchmark agreement on scenario-matched records",
              {"mean": i.bench_matched_mean, "min": i.bench_matched_min},
              f"mean >= {MIN_BENCH_MATCHED_MEAN} and min >= "
              f"{MIN_BENCH_MATCHED_FLOOR}",
              g4_pass,
              "wire benchmark-pointer matcher; append bench feedback"),
        _gate("G5", "similarity precision on labeled same/diff-cause pairs",
              g5_value,
              f"separation >= {MIN_SIMILARITY_SEPARATION} and diff-mean <= "
              f"{MAX_DIFF_CAUSE_MEAN}",
              g5_pass,
              "label 50 validated pairs; run offline similarity harness"),
        _gate("G6", "retrieval usefulness (mean IIP + PGS, 30d shadow)",
              {"mean_iip": i.mean_iip, "mean_pgs": i.mean_pgs},
              f"IIP >= {MIN_MEAN_IIP} and PGS >= {MIN_MEAN_PGS}",
              g6_pass,
              "run shadow window; compute nightly IIP/PGS"),
        _gate("G7", "regression ceiling (WRS/RCAS < -0.1 share)",
              i.regression_share, f"<= {MAX_REGRESSION_SHARE}",
              None if i.regression_share is None
              else i.regression_share <= MAX_REGRESSION_SHARE,
              "compute WRS/RCAS against baseline during shadow window"),
        _gate("G8", "false retrieval rate (labeled shadow sample)",
              i.false_retrieval_rate, f"<= {MAX_FALSE_RETRIEVAL_RATE}",
              None if i.false_retrieval_rate is None
              else i.false_retrieval_rate <= MAX_FALSE_RETRIEVAL_RATE,
              "label top-3 retrievals against validation ground truth"),
        _gate("G9", "confidence calibration (max 5-bin error)",
              i.max_calibration_bin_error, f"<= {MAX_CALIBRATION_BIN_ERROR}",
              None if i.max_calibration_bin_error is None
              else i.max_calibration_bin_error <= MAX_CALIBRATION_BIN_ERROR,
              "run ConfidenceCalibrator over admitted corpus"),
        _gate("G10", "latency invariance (p99 investigate() delta)",
              i.p99_latency_delta, f"< {MAX_P99_LATENCY_DELTA}",
              None if i.p99_latency_delta is None
              else i.p99_latency_delta < MAX_P99_LATENCY_DELTA,
              "capture 30-day p99 baseline with shadow flags ON"),
        _gate("G11", "fail-safe drill (retroactive demotion end-to-end)",
              i.failsafe_drill_completed, "completed once in staging",
              i.failsafe_drill_completed
              if i.failsafe_drill_completed is not None else None,
              "execute operator-rejection → quarantine drill in staging"),
    ]

    all_passed = all(g["passed"] for g in gates)
    return {
        "schema_version": READINESS_SCHEMA_VERSION,
        "gates": gates,
        "passed_count": sum(1 for g in gates if g["passed"]),
        "failed_count": sum(1 for g in gates if not g["passed"]),
        "all_passed": all_passed,
        # Hard invariant of the Readiness Program: evidence only.
        # Enabling Wave 3 is a human decision taken elsewhere; this
        # report can never flip it.
        "wave3_enabled": False,
        "verdict": "READY (pending human sign-off)" if all_passed
        else "NOT READY",
        "blocking_gates": [g["gate"] for g in gates if not g["passed"]],
    }


GATES = ("G1", "G2", "G3", "G4", "G5", "G6",
         "G7", "G8", "G9", "G10", "G11")

__all__ = ["GATES", "GateInputs", "READINESS_SCHEMA_VERSION",
           "evaluate_gates"]
