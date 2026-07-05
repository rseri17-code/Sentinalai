"""StrategyOptimizer — build one InvestigationStrategy per class."""
from __future__ import annotations

from collections import Counter
from typing import Iterable, Mapping

from sentinel_core.intel_memory import MemoryRecord
from sentinel_core.strategy_optimizer.cost_model import CostModel
from sentinel_core.strategy_optimizer.schemas import (
    InvestigationStrategy,
    StrategyStep,
    make_strategy_id,
)
from sentinel_core.strategy_optimizer.strategy_graph import StrategyGraph


class StrategyOptimizer:
    """Deterministic optimizer that emits a
    :class:`InvestigationStrategy` for the requested strategy class."""

    def __init__(
        self,
        cost_model: CostModel | None = None,
        registry: Mapping[str, tuple[str, ...]] | None = None,
    ) -> None:
        self._cost = cost_model or CostModel()
        # capability_id → tuple(skill names) — provided so cost model
        # can charge per-tool costs. Defaults to empty; callers pass a
        # SkillRegistry.to_dict() when they want per-tool costing.
        self._registry: dict[str, tuple[str, ...]] = {
            str(k): tuple(str(x) for x in v)
            for k, v in (registry or {}).items()
        }

    # ------------------------------------------------------------------
    # Per-step measurement
    # ------------------------------------------------------------------

    def evaluate_step(
        self,
        capability_id: str,
        graph: StrategyGraph,
        step_order: int = 0,
        prev_skill: str = "",
    ) -> StrategyStep:
        # Evidence typically produced (registry may not know this — we
        # approximate by graph co-occurrence)
        skills = self._registry.get(capability_id, ())
        prev = prev_skill or ""

        # Information gain: fraction of successful runs that used this cap
        cap_uses = graph.capability_count(capability_id)
        info_gain = round(cap_uses / max(1, graph.records_seen()), 4)

        # Historical success rate: fraction of runs using this cap that succeeded
        success = graph.capability_success_rate(capability_id)

        # Confidence gain: heuristic — cap use rate × 30 (bounded 0-100)
        confidence_gain = min(100, int(info_gain * 30 * 100))

        # MTTI reduction proxy: proportional to success rate × 30_000 ms
        mtti_reduction = int(success * 30_000)

        # Costs
        evidence_cost = self._cost.evidence_cost([])   # unknown at optimizer time
        tool_cost = self._cost.tool_cost(skills)
        switch = self._cost.switching_cost(prev, skills[0] if skills else "")
        exec_cost = tool_cost + switch

        value = self._cost.overall_value(
            info_gain, confidence_gain, success, exec_cost,
        )
        reason = (f"cap uses={cap_uses}, success_rate={success:.2f}, "
                    f"skills={list(skills)}")
        evidence_strs = (
            f"cap_uses={cap_uses}",
            f"success_rate={success:.2f}",
            f"tool_cost={tool_cost}",
            f"switching_cost={switch}",
        )
        return StrategyStep(
            capability_id=capability_id,
            step_order=int(step_order),
            expected_information_gain=info_gain,
            expected_confidence_gain=confidence_gain,
            expected_mtti_reduction_ms=mtti_reduction,
            evidence_cost=evidence_cost,
            tool_cost=tool_cost,
            execution_cost=exec_cost,
            historical_success_rate=success,
            overall_expected_value=value,
            reason=reason,
            evidence=evidence_strs,
        )

    # ------------------------------------------------------------------
    # Strategy assembly
    # ------------------------------------------------------------------

    def build_strategy(
        self,
        strategy_class: str,
        candidate_capabilities: tuple[str, ...],
        graph: StrategyGraph,
    ) -> InvestigationStrategy:
        # Score each capability
        steps: list[StrategyStep] = []
        prev_skill = ""
        for c in candidate_capabilities:
            step = self.evaluate_step(c, graph, step_order=0, prev_skill=prev_skill)
            steps.append(step)
            skills = self._registry.get(c, ())
            prev_skill = skills[0] if skills else prev_skill

        # Sort per strategy class
        if strategy_class == "fastest":
            sort_key = lambda s: (s.execution_cost, s.capability_id)
        elif strategy_class == "highest_confidence":
            sort_key = lambda s: (-s.expected_confidence_gain,
                                    -s.historical_success_rate,
                                    s.capability_id)
        elif strategy_class == "lowest_cost":
            sort_key = lambda s: (s.execution_cost + s.evidence_cost,
                                    s.capability_id)
        elif strategy_class == "highest_success":
            sort_key = lambda s: (-s.historical_success_rate,
                                    -s.overall_expected_value,
                                    s.capability_id)
        elif strategy_class == "balanced":
            sort_key = lambda s: (-s.overall_expected_value, s.capability_id)
        else:
            # "best" — default
            sort_key = lambda s: (-s.overall_expected_value,
                                    -s.historical_success_rate,
                                    s.capability_id)

        ordered = sorted(steps, key=sort_key)
        # Re-number
        ordered = [
            StrategyStep(**{**s.__dict__, "step_order": i + 1})
            for i, s in enumerate(ordered)
        ]
        ordered_tuple = tuple(ordered)

        total_mtti = sum(int(s.expected_mtti_reduction_ms) for s in ordered_tuple)
        total_cost = sum(int(s.execution_cost) for s in ordered_tuple)
        # Cumulative confidence, capped at 100
        cum = 0
        for s in ordered_tuple:
            cum = min(100, cum + int(s.expected_confidence_gain))
        overall_value = round(
            sum(float(s.overall_expected_value) for s in ordered_tuple) / max(1, len(ordered_tuple)),
            4,
        )
        strategy_id = make_strategy_id(strategy_class, ordered_tuple)

        return InvestigationStrategy(
            strategy_id=strategy_id,
            strategy_class=strategy_class,
            name=strategy_class.replace("_", " ").title() + " Strategy",
            steps=ordered_tuple,
            total_expected_mtti_ms=total_mtti,
            total_expected_cost=total_cost,
            total_expected_confidence=cum,
            overall_value=overall_value,
            reason=f"Ordered by {strategy_class}",
        )

    def build_all_strategies(
        self,
        candidate_capabilities: tuple[str, ...],
        graph: StrategyGraph,
    ) -> tuple[InvestigationStrategy, ...]:
        classes = ("best", "fastest", "highest_confidence",
                    "lowest_cost", "highest_success", "balanced")
        return tuple(self.build_strategy(c, candidate_capabilities, graph)
                       for c in classes)


__all__ = ["StrategyOptimizer"]
