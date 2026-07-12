"""P4 — Learning Effectiveness: longitudinal trend reports.

Answers "is SentinelAI actually improving?" with per-period series and
closed-form trend descriptors — proving learning, never assuming it.

Periods are ISO-8601 day buckets over MemoryRecord timestamps. Trend
descriptor mirrors the replay trend convention: slope over the series
with a ±dead-band, direction ∈ {improving, degrading, flat}. Whether a
positive slope is *good* depends on the metric (MTTI down = good;
quality up = good) — the report states the desired direction per metric
so readers never have to guess.
"""
from __future__ import annotations

from typing import Any, Iterable

EFFECTIVENESS_SCHEMA_VERSION = 1
_DEAD_BAND = 0.01

# metric → (extractor, desired_direction)
_METRICS: dict[str, tuple[str, str]] = {
    "mtti_ms":              ("mtti_ms", "down"),
    "mttr_ms":              ("mttr_ms", "down"),
    "rca_quality":          ("investigation_score", "up"),
    "worker_cost":          ("runtime_cost", "down"),
    "evidence_count":       ("_evidence_count", "down"),
    "confidence":           ("confidence", "up"),
    "benchmark_agreement":  ("sentinelbench_score", "up"),
}


def _slope(values: list[float]) -> float:
    """Least-squares slope over index order — pure arithmetic."""
    n = len(values)
    if n < 2:
        return 0.0
    xs = range(n)
    mean_x = (n - 1) / 2.0
    mean_y = sum(values) / n
    num = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, values))
    den = sum((x - mean_x) ** 2 for x in xs)
    return num / den if den else 0.0


def _trend(values: list[float], desired: str) -> dict[str, Any]:
    slope = _slope(values)
    # Normalize the dead-band against the series scale.
    scale = max((abs(v) for v in values), default=1.0) or 1.0
    rel = slope / scale
    if abs(rel) <= _DEAD_BAND:
        direction = "flat"
    else:
        direction = "up" if slope > 0 else "down"
    if direction == "flat":
        verdict = "flat"
    else:
        verdict = "improving" if direction == desired else "degrading"
    return {
        "slope": round(slope, 6),
        "direction": direction,
        "desired_direction": desired,
        "verdict": verdict,
        "first": round(values[0], 4) if values else None,
        "last": round(values[-1], 4) if values else None,
        "periods": len(values),
    }


def learning_effectiveness_report(
    records: Iterable[Any],
    replay_agreement_series: list[float] | None = None,
    calibration_error_series: list[float] | None = None,
    usefulness_series: list[float] | None = None,
) -> dict[str, Any]:
    """Per-day series + trend verdict per metric.

    The three optional series come from the nightly pipeline's own
    history (replay agreement, calibrator error, mean usefulness) —
    they are period-indexed already.
    """
    buckets: dict[str, list[Any]] = {}
    for r in records:
        day = str(r.timestamp)[:10]
        if day:
            buckets.setdefault(day, []).append(r)
    days = sorted(buckets)

    def _series(attr: str) -> list[float]:
        out = []
        for day in days:
            rows = buckets[day]
            if attr == "_evidence_count":
                vals = [float(len(r.evidence_collected)) for r in rows]
            else:
                vals = [float(getattr(r, attr, 0) or 0) for r in rows]
            out.append(round(sum(vals) / len(vals), 4) if vals else 0.0)
        return out

    trends: dict[str, Any] = {}
    series: dict[str, list[float]] = {}
    for name, (attr, desired) in sorted(_METRICS.items()):
        s = _series(attr)
        series[name] = s
        trends[name] = _trend(s, desired)

    for name, s, desired in (
        ("replay_agreement", replay_agreement_series or [], "up"),
        ("calibration_error", calibration_error_series or [], "down"),
        ("memory_usefulness", usefulness_series or [], "up"),
    ):
        series[name] = [round(float(v), 4) for v in s]
        trends[name] = _trend(series[name], desired)

    improving = sorted(n for n, t in trends.items()
                        if t["verdict"] == "improving")
    degrading = sorted(n for n, t in trends.items()
                        if t["verdict"] == "degrading")
    return {
        "schema_version": EFFECTIVENESS_SCHEMA_VERSION,
        "periods": days,
        "series": series,
        "trends": trends,
        "improving": improving,
        "degrading": degrading,
        "is_learning": bool(improving) and not degrading,
    }


__all__ = ["EFFECTIVENESS_SCHEMA_VERSION", "learning_effectiveness_report"]
