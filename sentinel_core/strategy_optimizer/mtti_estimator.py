"""MTTI estimator over a MemoryRecord corpus."""
from __future__ import annotations

from statistics import mean, pstdev
from typing import Iterable

from sentinel_core.intel_memory import MemoryRecord
from sentinel_core.strategy_optimizer.schemas import MttiEstimation


class MttiEstimator:
    """Compute expected MTTI + confidence interval from a memory corpus."""

    def estimate(
        self,
        records: Iterable[MemoryRecord],
        current_mtti_ms: int = 0,
    ) -> MttiEstimation:
        vals = [int(r.mtti_ms or 0) for r in (records or ())
                if int(r.mtti_ms or 0) > 0]
        n = len(vals)
        if n == 0:
            return MttiEstimation(
                current_mtti_ms=int(current_mtti_ms or 0),
                historical_mtti_ms=0,
                expected_mtti_ms=int(current_mtti_ms or 0),
                potential_improvement_ms=0,
                potential_improvement_pct=0.0,
                confidence_interval=(0, 0),
                sample_size=0,
            )
        hist = int(mean(vals))
        sd = int(pstdev(vals)) if n > 1 else 0
        # 68% CI ≈ mean ± sd (deterministic simple heuristic)
        lo = max(0, hist - sd)
        hi = hist + sd
        # "Expected" MTTI under the discovered strategy = min(historical
        # mean, current). If we don't know the current, use historical.
        current = int(current_mtti_ms or hist)
        expected = min(hist, current)
        improvement = max(0, current - expected)
        pct = round(improvement / current * 100.0, 4) if current > 0 else 0.0
        return MttiEstimation(
            current_mtti_ms=current,
            historical_mtti_ms=hist,
            expected_mtti_ms=expected,
            potential_improvement_ms=improvement,
            potential_improvement_pct=pct,
            confidence_interval=(lo, hi),
            sample_size=n,
        )


__all__ = ["MttiEstimator"]
