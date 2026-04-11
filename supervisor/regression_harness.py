"""Quality regression harness for SentinalAI.

Detects quality regressions between investigation batches by comparing
key metrics against a stored baseline.

Regression is detected when any monitored metric drops more than its
configured threshold relative to the baseline.

Metrics tracked:
  - accuracy            (exact + partial root cause matches / total)
  - calibration_error   (ECE — expected calibration error, lower is better)
  - evidence_coverage   (fraction of required evidence sources found)
  - citation_coverage   (fraction of RCA claims with source citations)
  - false_positive_rate (fraction with confidence < 30)

The baseline is stored as JSON at REGRESSION_BASELINE_PATH and is updated
after each successful evaluation batch IF the new metrics represent an
improvement (ratchet behaviour — baseline only moves forward).

Configuration:
  REGRESSION_BASELINE_PATH      — JSON file (default: eval/regression_baseline.json)
  REGRESSION_ACCURACY_THRESH    — max allowed drop in accuracy (default: 0.05)
  REGRESSION_CALIBRATION_THRESH — max allowed rise in ECE (default: 0.10)
  REGRESSION_COVERAGE_THRESH    — max allowed drop in evidence coverage (default: 0.08)
  REGRESSION_ENABLED            — enable/disable (default: true)
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass, asdict, field
from typing import Any

logger = logging.getLogger("sentinalai.regression_harness")

REGRESSION_ENABLED = os.environ.get(
    "REGRESSION_ENABLED", "true"
).lower() in ("1", "true", "yes")

BASELINE_PATH = os.environ.get(
    "REGRESSION_BASELINE_PATH",
    os.path.join(os.path.dirname(__file__), "..", "eval", "regression_baseline.json"),
)

# Regression thresholds — how much a metric can degrade before alerting
THRESHOLDS: dict[str, float] = {
    "accuracy":          float(os.environ.get("REGRESSION_ACCURACY_THRESH",    "0.05")),
    "evidence_coverage": float(os.environ.get("REGRESSION_COVERAGE_THRESH",    "0.08")),
    "citation_coverage": float(os.environ.get("REGRESSION_CITATION_THRESH",    "0.08")),
    "false_positive_rate": float(os.environ.get("REGRESSION_FP_THRESH",        "0.05")),
    # ECE: lower is better — regression = ECE rises
    "calibration_error": float(os.environ.get("REGRESSION_CALIBRATION_THRESH", "0.10")),
}

_lock = threading.Lock()


@dataclass
class RegressionBaseline:
    """Snapshot of quality metrics used as comparison baseline."""

    accuracy: float = 0.0
    calibration_error: float = 1.0   # ECE: starts high (unknown)
    evidence_coverage: float = 0.0
    citation_coverage: float = 0.0
    false_positive_rate: float = 1.0
    sample_count: int = 0
    recorded_at: str = ""
    version: str = "1"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RegressionReport:
    """Result of comparing current metrics against baseline."""

    has_regression: bool = False
    regressions: list[dict[str, Any]] = field(default_factory=list)
    improvements: list[dict[str, Any]] = field(default_factory=list)
    current_metrics: dict[str, float] = field(default_factory=dict)
    baseline_metrics: dict[str, float] = field(default_factory=dict)
    checked_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> str:
        if not self.has_regression:
            return f"✓ No regressions detected ({len(self.improvements)} improvements)"
        reg_strs = [
            f"{r['metric']}: {r['baseline']:.3f} → {r['current']:.3f} (Δ{r['delta']:+.3f})"
            for r in self.regressions
        ]
        return "✗ REGRESSIONS: " + "; ".join(reg_strs)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def check_regression(current_metrics: dict[str, float]) -> RegressionReport:
    """Compare current metrics against the stored baseline.

    Args:
        current_metrics: dict with keys matching THRESHOLDS

    Returns:
        RegressionReport — has_regression=True if any metric regressed
    """
    if not REGRESSION_ENABLED:
        return RegressionReport(checked_at=_now())

    baseline = load_baseline()
    baseline_dict = baseline.to_dict()

    regressions: list[dict] = []
    improvements: list[dict] = []

    for metric, threshold in THRESHOLDS.items():
        current_val = current_metrics.get(metric)
        if current_val is None:
            continue

        baseline_val = baseline_dict.get(metric)
        if baseline_val is None or baseline.sample_count == 0:
            continue  # no baseline yet — skip

        # Higher is better for all metrics EXCEPT calibration_error and false_positive_rate
        higher_is_better = metric not in ("calibration_error", "false_positive_rate")

        if higher_is_better:
            delta = current_val - baseline_val
            regressed = delta < -threshold
        else:
            delta = current_val - baseline_val
            regressed = delta > threshold

        entry = {
            "metric": metric,
            "baseline": round(baseline_val, 4),
            "current": round(current_val, 4),
            "delta": round(delta, 4),
            "threshold": threshold,
        }

        if regressed:
            regressions.append(entry)
            logger.warning(
                "REGRESSION: %s baseline=%.3f current=%.3f delta=%+.3f threshold=%.3f",
                metric, baseline_val, current_val, delta, threshold,
            )
        elif abs(delta) > 0.01:
            improvements.append(entry)

    report = RegressionReport(
        has_regression=bool(regressions),
        regressions=regressions,
        improvements=improvements,
        current_metrics={k: round(v, 4) for k, v in current_metrics.items()},
        baseline_metrics={
            k: round(baseline_dict.get(k, 0.0), 4)
            for k in THRESHOLDS
        },
        checked_at=_now(),
    )

    if report.has_regression:
        logger.error("Quality regression detected: %s", report.summary())
    else:
        logger.info("Regression check passed: %s", report.summary())

    return report


def update_baseline_if_better(current_metrics: dict[str, float], sample_count: int) -> bool:
    """Update baseline if current metrics are overall better (ratchet).

    The baseline only moves forward — it never degrades. Uses a simple
    aggregate score: accuracy + evidence_coverage - calibration_error.

    Returns True if baseline was updated.
    """
    if not REGRESSION_ENABLED or sample_count < 5:
        return False

    with _lock:
        baseline = load_baseline()

        def _score(m: dict) -> float:
            return (
                m.get("accuracy", 0.0)
                + m.get("evidence_coverage", 0.0)
                + m.get("citation_coverage", 0.0)
                - m.get("calibration_error", 1.0)
                - m.get("false_positive_rate", 1.0)
            )

        current_score = _score(current_metrics)
        baseline_score = _score(baseline.to_dict())

        if current_score <= baseline_score and baseline.sample_count > 0:
            logger.info(
                "Baseline not updated (current_score=%.3f <= baseline_score=%.3f)",
                current_score, baseline_score,
            )
            return False

        new_baseline = RegressionBaseline(
            accuracy=current_metrics.get("accuracy", baseline.accuracy),
            calibration_error=current_metrics.get("calibration_error", baseline.calibration_error),
            evidence_coverage=current_metrics.get("evidence_coverage", baseline.evidence_coverage),
            citation_coverage=current_metrics.get("citation_coverage", baseline.citation_coverage),
            false_positive_rate=current_metrics.get("false_positive_rate", baseline.false_positive_rate),
            sample_count=sample_count,
            recorded_at=_now(),
        )
        _save_baseline(new_baseline)
        logger.info(
            "Baseline updated: score %.3f → %.3f (n=%d)",
            baseline_score, current_score, sample_count,
        )
        return True


def load_baseline() -> RegressionBaseline:
    """Load baseline from disk. Returns empty baseline if not found."""
    try:
        if os.path.exists(BASELINE_PATH):
            with open(BASELINE_PATH) as f:
                data = json.load(f)
            return RegressionBaseline(**{
                k: v for k, v in data.items()
                if k in RegressionBaseline.__dataclass_fields__
            })
    except Exception as exc:
        logger.debug("Could not load regression baseline: %s", exc)
    return RegressionBaseline()


def _save_baseline(baseline: RegressionBaseline) -> None:
    """Atomically write baseline to disk."""
    tmp = BASELINE_PATH + ".tmp"
    os.makedirs(os.path.dirname(BASELINE_PATH), exist_ok=True)
    with open(tmp, "w") as f:
        json.dump(baseline.to_dict(), f, indent=2)
    os.replace(tmp, BASELINE_PATH)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
