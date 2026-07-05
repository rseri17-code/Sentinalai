"""ReplayRunner — orchestrates offline replays against SentinelBench.

Reuses ``tests.synthetic.runner`` for scoring. Never invokes external
systems; never touches production runtime.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping

from tests.replay.replay_store import ReplayStore
from tests.replay.schemas import (
    BenchmarkRun,
    ReplayResult,
    Verdict,
)
from tests.synthetic.runner import (
    load_all_scenarios,
    load_scenario,
    run_all_scenarios,
    run_scenario,
)
from tests.synthetic.scoring import ScoreCard


_DIMENSIONS: tuple[str, ...] = (
    "root_cause_match",
    "evidence_completeness",
    "red_herring_resistance",
    "confidence_calibration",
    "decision_trace_quality",
    "runtime_cost_score",
    "mtti_score",
    "overall_score",
)


class ReplayRunner:
    """Orchestrator that runs SentinelBench replays and compares against
    a stored baseline."""

    def __init__(self, store: ReplayStore | None = None) -> None:
        self._store = store

    # ------------------------------------------------------------------
    # Core replay operations
    # ------------------------------------------------------------------

    def replay_scenario(
        self,
        scenario_id: str,
        investigation_output: Mapping[str, Any] | None = None,
        baseline_run_id: str | None = None,
        regression_threshold: float = 0.05,
    ) -> ReplayResult:
        """Replay a single scenario. Compare against optional baseline
        run's scorecard for the same scenario_id."""
        current = run_scenario(scenario_id, investigation_output)
        baseline: ScoreCard | None = None
        if baseline_run_id and self._store is not None:
            base_run = self._store.load(baseline_run_id)
            baseline = _find_card(base_run.scorecards, scenario_id)
        return self._to_result(current, baseline, regression_threshold)

    def replay_corpus(
        self,
        investigation_outputs: Mapping[str, Mapping[str, Any]] | None = None,
        filter_service: str | None = None,
        filter_incident_type: str | None = None,
        filter_tags: Iterable[str] | None = None,
        baseline_run_id: str | None = None,
        regression_threshold: float = 0.05,
    ) -> tuple[ReplayResult, ...]:
        """Replay every scenario. Optional filters against scenario
        attributes.

        Returns ReplayResults sorted by scenario_id.
        """
        scenarios = load_all_scenarios()
        service = str(filter_service or "")
        itype   = str(filter_incident_type or "")
        tags    = set(str(t) for t in (filter_tags or ()))

        selected_ids: list[str] = []
        for sid, sc in scenarios.items():
            if service and str(sc.incident_input.get("service", "")) != service:
                continue
            if itype and str(sc.incident_input.get("incident_type", "")) != itype:
                continue
            if tags and not tags.issubset(set(sc.tags)):
                continue
            selected_ids.append(sid)

        base_cards: dict[str, ScoreCard] = {}
        if baseline_run_id and self._store is not None:
            base_run = self._store.load(baseline_run_id)
            base_cards = {c.scenario_id: c for c in base_run.scorecards}

        outs = investigation_outputs or {}
        results: list[ReplayResult] = []
        for sid in sorted(selected_ids):
            io = outs.get(sid) if outs else None
            current = run_scenario(sid, io)
            results.append(self._to_result(
                current, base_cards.get(sid), regression_threshold,
            ))
        return tuple(results)

    def replay_by_date_range(
        self,
        start_iso: str,
        end_iso: str,
    ) -> tuple[BenchmarkRun, ...]:
        """Return stored benchmark runs whose generated_at is within
        the date range. Delegates to the store."""
        if self._store is None:
            return ()
        return self._store.load_in_date_range(start_iso, end_iso)

    # ------------------------------------------------------------------
    # Run capture
    # ------------------------------------------------------------------

    def capture_run(
        self,
        run_id: str,
        generated_at: str = "",
        investigation_outputs: Mapping[str, Mapping[str, Any]] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> BenchmarkRun:
        """Run every scenario against the given investigation outputs and
        return a :class:`BenchmarkRun`. If a store is attached, persist it.

        ``generated_at`` is caller-supplied so tests remain deterministic.
        """
        cards = run_all_scenarios(investigation_outputs=investigation_outputs)
        run = BenchmarkRun(
            run_id=str(run_id),
            generated_at=str(generated_at or ""),
            scorecards=tuple(cards),
            metadata=dict(metadata or {}),
        )
        if self._store is not None:
            self._store.save(run)
        return run

    # ------------------------------------------------------------------
    # Comparison utilities
    # ------------------------------------------------------------------

    def compare_runs(
        self, run_id_a: str, run_id_b: str
    ) -> dict[str, Any]:
        """Compare two runs. Returns a per-scenario delta table + summary."""
        if self._store is None:
            raise RuntimeError("compare_runs requires a store")
        a = self._store.load(run_id_a)
        b = self._store.load(run_id_b)
        a_by_id = {c.scenario_id: c for c in a.scorecards}
        b_by_id = {c.scenario_id: c for c in b.scorecards}
        ids = sorted(set(a_by_id.keys()) | set(b_by_id.keys()))
        per_scenario: list[dict[str, Any]] = []
        for sid in ids:
            ca = a_by_id.get(sid)
            cb = b_by_id.get(sid)
            row: dict[str, Any] = {"scenario_id": sid}
            for d in _DIMENSIONS:
                av = getattr(ca, d, 0.0) if ca is not None else 0.0
                bv = getattr(cb, d, 0.0) if cb is not None else 0.0
                row[d] = {
                    "a":     round(av, 4),
                    "b":     round(bv, 4),
                    "delta": round(bv - av, 4),
                }
            per_scenario.append(row)
        return {
            "run_a":         run_id_a,
            "run_b":         run_id_b,
            "per_scenario":  per_scenario,
        }

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _to_result(
        current: ScoreCard,
        baseline: ScoreCard | None,
        threshold: float,
    ) -> ReplayResult:
        if baseline is None:
            return ReplayResult(
                scenario_id=current.scenario_id,
                verdict=Verdict.NEW.value,
                current=current,
                baseline=None,
                delta={},
            )
        delta = {
            d: round(float(getattr(current, d)) - float(getattr(baseline, d)), 4)
            for d in _DIMENSIONS
        }
        overall_delta = delta["overall_score"]
        if overall_delta > threshold:
            verdict = Verdict.IMPROVED.value
        elif overall_delta < -threshold:
            verdict = Verdict.REGRESSED.value
        else:
            verdict = Verdict.STABLE.value
        return ReplayResult(
            scenario_id=current.scenario_id,
            verdict=verdict,
            current=current,
            baseline=baseline,
            delta=delta,
        )


def _find_card(cards, scenario_id: str):
    for c in cards:
        if c.scenario_id == scenario_id:
            return c
    return None


__all__ = [
    "ReplayRunner",
]
