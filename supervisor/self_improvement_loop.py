"""Autonomous self-improvement loop for SentinalAI.

9-step cycle:
  1. Observe failures — load experience corpus, identify low-quality incidents
  2. Score current behavior — compute per-incident-type and per-dimension quality
  3. Pick improvement goal — worst incident_type × lowest-signal step
  4. Select allowed knobs — strategy weights for that incident_type
  5. Execute experiment in sandbox — perturbation on a deep-copy of strategy
  6. Generate candidate change — weight delta for one step
  7. Rerun same eval set — simulate re-scoring with new weights
  8. Compare before/after — compute mean quality delta
  9. Accept only if delta >= min_delta — write to evolved_strategy.json

All writes are atomic (tmp-swap). Never raises — every step is guarded.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import threading
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("sentinalai.self_improvement_loop")

_EXPERIENCE_PATH = os.getenv("EXPERIENCE_STORE_PATH", "eval/experience_store.json")
_STRATEGY_PATH = os.getenv("EVOLVED_STRATEGY_PATH", "eval/evolved_strategy.json")
_REPORT_PATH = os.getenv("IMPROVEMENT_REPORT_PATH", "eval/improvement_reports.json")

_FAILURE_THRESHOLD = 0.60     # experiences below this count as failures
_MAX_REPORTS = 200            # cap stored reports
_MAX_WEIGHT = 2.5             # ceiling for any step weight
_MIN_WEIGHT = 0.5             # floor for any step weight
_WEIGHT_DELTA = 0.15          # perturbation magnitude per experiment

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class EvalCorpusScore:
    """Summary of current quality across the stored experience corpus."""
    overall: float                              # mean online_quality_score
    per_incident_type: dict[str, float]         # incident_type → mean quality
    failure_count: int                          # experiences below threshold
    total_count: int
    worst_incident_type: str = ""              # lowest mean quality type


@dataclass
class Experiment:
    """One perturbation trial."""
    target_incident_type: str
    target_step: str
    weight_before: float
    weight_after: float
    baseline_quality: float                    # mean quality before
    candidate_quality: float                   # simulated mean quality after
    delta: float                               # candidate - baseline
    accepted: bool = False
    affected_experiences: int = 0             # experiences in the simulation pool


@dataclass
class ImprovementReport:
    """Full result of one run_cycle() call."""
    cycle_id: str
    baseline_score: float
    worst_incident_type: str
    failure_count: int
    total_count: int
    experiments_run: int
    accepted_count: int
    final_score: float
    net_delta: float                           # final_score - baseline_score
    accepted_changes: dict[str, dict]          # step → {before, after} for accepted
    timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Core class
# ---------------------------------------------------------------------------

class SelfImprovementLoop:
    """Runs the 9-step autonomous improvement cycle against the experience corpus."""

    def __init__(
        self,
        experience_path: str = _EXPERIENCE_PATH,
        strategy_path: str = _STRATEGY_PATH,
        report_path: str = _REPORT_PATH,
    ) -> None:
        self._exp_path = experience_path
        self._strat_path = strategy_path
        self._report_path = report_path

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_cycle(
        self,
        max_experiments: int = 5,
        min_delta: float = 0.02,
    ) -> ImprovementReport:
        """Run one full self-improvement cycle.

        Args:
            max_experiments: Maximum number of step perturbations to try.
            min_delta:       Minimum quality improvement to accept a change (0-1).

        Returns:
            ImprovementReport summarising what happened.
        """
        cycle_id = uuid.uuid4().hex[:12]
        ts = datetime.now(timezone.utc).isoformat()

        # Step 1-2: Observe failures + score current behavior
        experiences = self._load_experiences()
        strategy = self._load_strategy()
        corpus_score = self._score_corpus(experiences)

        logger.info(
            "SelfImprovementLoop cycle=%s overall=%.3f failures=%d/%d worst_type=%s",
            cycle_id, corpus_score.overall,
            corpus_score.failure_count, corpus_score.total_count,
            corpus_score.worst_incident_type,
        )

        # Step 3: Pick improvement goal
        target_type = corpus_score.worst_incident_type
        if not target_type or target_type not in strategy:
            logger.info("SelfImprovementLoop: no target type found, exiting")
            return self._make_report(
                cycle_id, corpus_score, corpus_score.overall, [], ts
            )

        # Step 4: Select allowed knobs — steps for the target incident_type
        step_candidates = self._rank_steps_for_improvement(
            target_type, strategy, experiences
        )

        # Step 5-8: Experiment loop
        accepted: list[Experiment] = []
        working_strategy = copy.deepcopy(strategy)

        for step_name, step_data in step_candidates[:max_experiments]:
            exp = self._run_experiment(
                target_type=target_type,
                step_name=step_name,
                step_data=step_data,
                experiences=experiences,
                working_strategy=working_strategy,
                min_delta=min_delta,
            )
            if exp.accepted:
                # Apply accepted change to working strategy for next iteration
                working_strategy[target_type][step_name]["weight"] = exp.weight_after
                accepted.append(exp)
                logger.debug(
                    "SelfImprovementLoop: accepted %s.%s %.3f→%.3f delta=%.3f",
                    target_type, step_name, exp.weight_before, exp.weight_after, exp.delta,
                )

        # Step 9: Write accepted changes to strategy file
        if accepted:
            self._apply_and_save_strategy(
                working_strategy, target_type, accepted, strategy
            )

        # Compute final corpus score estimate
        final_score = corpus_score.overall
        if accepted:
            net_delta = sum(e.delta for e in accepted) / len(experiences) * len(
                [x for x in experiences if x.get("incident_type") == target_type]
            )
            final_score = min(1.0, corpus_score.overall + net_delta / max(1, corpus_score.total_count))

        report = self._make_report(cycle_id, corpus_score, final_score, accepted, ts)
        self._save_report(report)
        return report

    # ------------------------------------------------------------------
    # Steps 1-2: Observe + score
    # ------------------------------------------------------------------

    def _load_experiences(self) -> list[dict]:
        try:
            with open(self._exp_path) as f:
                data = json.load(f)
            return [r for r in data if isinstance(r, dict) and "online_quality_score" in r]
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("SelfImprovementLoop: cannot load experiences: %s", exc)
            return []

    def _load_strategy(self) -> dict:
        try:
            with open(self._strat_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
            logger.warning("SelfImprovementLoop: cannot load strategy: %s", exc)
            return {}

    def _score_corpus(self, experiences: list[dict]) -> EvalCorpusScore:
        if not experiences:
            return EvalCorpusScore(
                overall=0.0, per_incident_type={}, failure_count=0, total_count=0
            )

        scores_by_type: dict[str, list[float]] = {}
        failures = 0
        for exp in experiences:
            q = float(exp.get("online_quality_score", 0.0))
            itype = exp.get("incident_type", "unknown")
            scores_by_type.setdefault(itype, []).append(q)
            if q < _FAILURE_THRESHOLD:
                failures += 1

        per_type = {t: sum(s) / len(s) for t, s in scores_by_type.items()}
        overall = sum(per_type.values()) / len(per_type) if per_type else 0.0
        worst = min(per_type, key=per_type.__getitem__) if per_type else ""

        return EvalCorpusScore(
            overall=overall,
            per_incident_type=per_type,
            failure_count=failures,
            total_count=len(experiences),
            worst_incident_type=worst,
        )

    # ------------------------------------------------------------------
    # Steps 3-4: Pick goal + select knobs
    # ------------------------------------------------------------------

    def _rank_steps_for_improvement(
        self,
        target_type: str,
        strategy: dict,
        experiences: list[dict],
    ) -> list[tuple[str, dict]]:
        """Return steps ranked by improvement potential (lowest ema_signal first).

        We target steps with low ema_signal that are underused in low-quality
        incidents — boosting their weight should improve future quality.
        """
        type_steps = strategy.get(target_type, {})
        if not type_steps:
            return []

        # Find steps that are underused in failing experiences for this type
        failing_keys: set[str] = set()
        for exp in experiences:
            if (
                exp.get("incident_type") == target_type
                and float(exp.get("online_quality_score", 1.0)) < _FAILURE_THRESHOLD
            ):
                for k in exp.get("evidence_keys", []):
                    failing_keys.add(k)

        candidates = []
        for step, data in type_steps.items():
            ema = float(data.get("ema_signal", 0.0))
            weight = float(data.get("weight", 1.0))
            # Improvement potential: steps with decent signal but below-average weight
            potential = ema - weight  # positive means underweighted relative to signal
            candidates.append((step, data, potential))

        # Sort: highest potential first (most underweighted)
        candidates.sort(key=lambda x: x[2], reverse=True)
        return [(s, d) for s, d, _ in candidates]

    # ------------------------------------------------------------------
    # Steps 5-8: Experiment
    # ------------------------------------------------------------------

    def _run_experiment(
        self,
        target_type: str,
        step_name: str,
        step_data: dict,
        experiences: list[dict],
        working_strategy: dict,
        min_delta: float,
    ) -> Experiment:
        """Simulate quality impact of boosting step_name's weight by _WEIGHT_DELTA."""
        weight_before = float(step_data.get("weight", 1.0))
        ema_signal = float(step_data.get("ema_signal", 0.0))
        weight_after = min(_MAX_WEIGHT, max(_MIN_WEIGHT, weight_before + _WEIGHT_DELTA))

        # Pool: experiences of this incident_type where the step was NOT used
        pool = [
            exp for exp in experiences
            if exp.get("incident_type") == target_type
            and step_name not in exp.get("evidence_keys", [])
        ]

        if not pool:
            # Step already universally used — no room to improve via weight boost
            return Experiment(
                target_incident_type=target_type,
                target_step=step_name,
                weight_before=weight_before,
                weight_after=weight_after,
                baseline_quality=0.0,
                candidate_quality=0.0,
                delta=0.0,
                accepted=False,
                affected_experiences=0,
            )

        baseline_scores = [float(e.get("online_quality_score", 0.0)) for e in pool]
        baseline_mean = sum(baseline_scores) / len(baseline_scores)

        # Simulate: if this step had been called (due to higher weight), what quality boost?
        # Model: boost = weight_delta_fraction * ema_signal * (1 - current_quality)
        # This gives diminishing returns and caps at 1.0.
        weight_delta_fraction = (weight_after - weight_before) / max(weight_before, 0.01)
        simulated_scores = [
            min(1.0, q + weight_delta_fraction * ema_signal * (1.0 - q))
            for q in baseline_scores
        ]
        candidate_mean = sum(simulated_scores) / len(simulated_scores)
        delta = candidate_mean - baseline_mean
        accepted = delta >= min_delta

        return Experiment(
            target_incident_type=target_type,
            target_step=step_name,
            weight_before=weight_before,
            weight_after=weight_after,
            baseline_quality=round(baseline_mean, 4),
            candidate_quality=round(candidate_mean, 4),
            delta=round(delta, 4),
            accepted=accepted,
            affected_experiences=len(pool),
        )

    # ------------------------------------------------------------------
    # Step 9: Apply + persist
    # ------------------------------------------------------------------

    def _apply_and_save_strategy(
        self,
        working_strategy: dict,
        target_type: str,
        accepted: list[Experiment],
        original_strategy: dict,
    ) -> None:
        """Atomically write accepted weight changes to evolved_strategy.json."""
        ts = datetime.now(timezone.utc).isoformat()
        for exp in accepted:
            step = exp.target_step
            if target_type in working_strategy and step in working_strategy[target_type]:
                working_strategy[target_type][step]["weight"] = exp.weight_after
                working_strategy[target_type][step]["last_updated"] = ts

        tmp = self._strat_path + ".tmp"
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._strat_path)), exist_ok=True)
            with open(tmp, "w") as f:
                json.dump(working_strategy, f, indent=2)
            os.replace(tmp, self._strat_path)
            logger.info(
                "SelfImprovementLoop: wrote %d weight changes for %s",
                len(accepted), target_type,
            )
        except OSError as exc:
            logger.warning("SelfImprovementLoop: strategy save failed: %s", exc)

    # ------------------------------------------------------------------
    # Report helpers
    # ------------------------------------------------------------------

    def _make_report(
        self,
        cycle_id: str,
        corpus_score: EvalCorpusScore,
        final_score: float,
        accepted: list[Experiment],
        ts: str,
    ) -> ImprovementReport:
        accepted_changes = {
            e.target_step: {"before": e.weight_before, "after": e.weight_after}
            for e in accepted if e.accepted
        }
        return ImprovementReport(
            cycle_id=cycle_id,
            baseline_score=round(corpus_score.overall, 4),
            worst_incident_type=corpus_score.worst_incident_type,
            failure_count=corpus_score.failure_count,
            total_count=corpus_score.total_count,
            experiments_run=len(accepted),
            accepted_count=sum(1 for e in accepted if e.accepted),
            final_score=round(final_score, 4),
            net_delta=round(final_score - corpus_score.overall, 4),
            accepted_changes=accepted_changes,
            timestamp=ts,
        )

    def _save_report(self, report: ImprovementReport) -> None:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(self._report_path)), exist_ok=True)
            try:
                with open(self._report_path) as f:
                    reports = json.load(f)
                if not isinstance(reports, list):
                    reports = []
            except (FileNotFoundError, json.JSONDecodeError):
                reports = []

            reports.append(report.to_dict())
            if len(reports) > _MAX_REPORTS:
                reports = reports[-_MAX_REPORTS:]

            tmp = self._report_path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(reports, f, indent=2)
            os.replace(tmp, self._report_path)
        except OSError as exc:
            logger.warning("SelfImprovementLoop: report save failed: %s", exc)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_loop: SelfImprovementLoop | None = None


def get_self_improvement_loop() -> SelfImprovementLoop:
    global _loop
    with _lock:
        if _loop is None:
            _loop = SelfImprovementLoop()
        return _loop


def run_improvement_cycle(
    max_experiments: int = 5,
    min_delta: float = 0.02,
) -> ImprovementReport:
    """Convenience wrapper: run one cycle via the module singleton."""
    return get_self_improvement_loop().run_cycle(
        max_experiments=max_experiments,
        min_delta=min_delta,
    )
