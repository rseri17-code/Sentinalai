"""Loop engineering for the agentic investigation planner.

Wraps AgenticPlanner with three additional control layers:
  1. Pre-loop seeding — inject similar past episode root-causes as starting hypotheses
  2. Quality-gated convergence — stop early when evidence quality exceeds threshold
  3. Nudge-before-break stagnation handling — redirect instead of hard-stopping

Exposes LoopTelemetry per investigation for the /api/loop/metrics endpoint.

Feature-flagged via LOOP_CONTROLLER_ENABLED (default: false).
Convergence threshold: LOOP_CONVERGENCE_THRESHOLD (default: 0.72).
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

LOOP_CONVERGENCE_THRESHOLD = float(os.environ.get("LOOP_CONVERGENCE_THRESHOLD", "0.72"))
LOOP_MAX_NUDGES = int(os.environ.get("LOOP_MAX_NUDGES", "2"))

# Process-level store keyed by investigation_id (for /api/loop/metrics)
_telemetry_store: dict[str, "LoopTelemetry"] = {}
_store_lock = Lock()

# Expected evidence keys by incident type — used for tool-coverage scoring
_EXPECTED_KEYS: dict[str, list[str]] = {
    "latency":         ["apm_traces", "splunk_logs", "metrics", "cmdb_blast_radius"],
    "error_rate":      ["error_analysis", "splunk_logs", "metrics", "stack_trace"],
    "pod_restart":     ["k8s_events", "k8s_pod_logs", "metrics", "cmdb_blast_radius"],
    "disk_full":       ["disk_usage", "k8s_events", "metrics"],
    "connection_pool": ["db_metrics", "pool_stats", "cmdb_blast_radius", "apm_traces"],
    "oom":             ["k8s_events", "k8s_pod_logs", "metrics", "heap_dump"],
    "default":         ["splunk_logs", "metrics", "cmdb_blast_radius"],
}

# Evidence keys that signal a root cause has been found
_ROOT_CAUSE_KEYS = frozenset([
    "root_cause", "hypothesis", "failure_mode", "rca_summary",
    "causal_chain", "primary_cause", "contributing_factors",
])


# ---------------------------------------------------------------------------
# Telemetry
# ---------------------------------------------------------------------------

@dataclass
class IterationRecord:
    iteration: int
    worker: str
    action: str
    quality: float
    nudge_sent: bool = False


@dataclass
class LoopTelemetry:
    """Audit record for one loop controller run."""
    investigation_id: str = ""
    incident_type: str = ""
    iterations_run: int = 0
    convergence_iter: int | None = None
    quality_per_iter: list[float] = field(default_factory=list)
    nudge_count: int = 0
    stagnation_detected: bool = False
    pre_seeded_keys: list[str] = field(default_factory=list)
    elapsed_ms: float = 0.0
    final_quality: float = 0.0
    iteration_records: list[IterationRecord] = field(default_factory=list)

    @property
    def mtti_ms(self) -> float:
        """Mean Time To Insight — elapsed until convergence or end."""
        if self.convergence_iter is not None and self.quality_per_iter:
            # Approximate: linear fraction of elapsed time
            frac = (self.convergence_iter + 1) / max(len(self.quality_per_iter), 1)
            return self.elapsed_ms * frac
        return self.elapsed_ms

    def to_dict(self) -> dict:
        return {
            "investigation_id": self.investigation_id,
            "incident_type": self.incident_type,
            "iterations_run": self.iterations_run,
            "convergence_iter": self.convergence_iter,
            "quality_per_iter": [round(q, 3) for q in self.quality_per_iter],
            "final_quality": round(self.final_quality, 3),
            "nudge_count": self.nudge_count,
            "stagnation_detected": self.stagnation_detected,
            "pre_seeded_keys": self.pre_seeded_keys,
            "mtti_ms": round(self.mtti_ms, 1),
            "elapsed_ms": round(self.elapsed_ms, 1),
        }


def get_telemetry(investigation_id: str) -> LoopTelemetry | None:
    with _store_lock:
        return _telemetry_store.get(investigation_id)


def list_telemetry(limit: int = 20) -> list[dict]:
    with _store_lock:
        items = list(_telemetry_store.values())
    return [t.to_dict() for t in items[-limit:]]


def clear_telemetry() -> int:
    with _store_lock:
        n = len(_telemetry_store)
        _telemetry_store.clear()
    return n


# ---------------------------------------------------------------------------
# Evidence quality scorer
# ---------------------------------------------------------------------------

class EvidenceQualityScorer:
    """Heuristic quality score for current evidence dict (no LLM calls)."""

    def score(self, incident_type: str, evidence: dict) -> float:
        ev_keys = {k for k in evidence if not k.startswith("_") and not evidence[k] or
                   k for k in evidence if not k.startswith("_")}
        # Recompute cleanly
        ev_keys = {k for k in evidence if not k.startswith("_")}
        error_keys = {k for k in ev_keys if isinstance(evidence.get(k), dict) and evidence[k].get("error")}
        signal_keys = ev_keys - error_keys

        # 1. Tool coverage: how many expected keys are present
        expected = _EXPECTED_KEYS.get(incident_type, _EXPECTED_KEYS["default"])
        present = sum(1 for k in expected if any(k in sk for sk in signal_keys))
        tool_coverage = present / len(expected) if expected else 0.0

        # 2. Signal ratio: non-error / total non-meta keys
        total = len(ev_keys)
        signal_ratio = len(signal_keys) / total if total else 0.0

        # 3. Root cause indicators: are any RCA-bearing keys present?
        rca_present = any(rk in signal_keys or any(rk in sk for sk in signal_keys)
                          for rk in _ROOT_CAUSE_KEYS)
        rca_score = 1.0 if rca_present else 0.0

        return min(1.0, 0.40 * tool_coverage + 0.35 * signal_ratio + 0.25 * rca_score)


# ---------------------------------------------------------------------------
# Main loop controller
# ---------------------------------------------------------------------------

class LoopController:
    """Quality-gated, nudge-capable agentic investigation loop.

    Integrates with AgenticPlanner but adds:
    - Pre-loop context seeding from EpisodicMemory
    - Per-iteration quality scoring with convergence detection
    - Nudge-before-break stagnation handling
    - LoopTelemetry recording for observability
    """

    def __init__(
        self,
        workers: dict[str, Any],
        llm_fn: Any,
        budget: Any,
        fallback_playbook: list[dict] | None = None,
        max_iterations: int = 10,
        convergence_threshold: float = LOOP_CONVERGENCE_THRESHOLD,
    ) -> None:
        self._workers = workers
        self._llm_fn = llm_fn
        self._budget = budget
        self._fallback_playbook = fallback_playbook or []
        self._max_iterations = max_iterations
        self._convergence_threshold = convergence_threshold
        self._scorer = EvidenceQualityScorer()

    def run(
        self,
        incident_id: str,
        incident: dict,
        incident_type: str,
        investigation_id: str = "",
        seed_evidence: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], LoopTelemetry]:
        telemetry = LoopTelemetry(
            investigation_id=investigation_id or incident_id,
            incident_type=incident_type,
        )
        t0 = time.monotonic()

        # Pre-loop: seed from EpisodicMemory
        seed = self._seed_from_memory(incident, incident_type, seed_evidence or {})
        telemetry.pre_seeded_keys = [k for k in seed if not k.startswith("_")]
        evidence: dict[str, Any] = dict(seed)

        from supervisor.planner import AgenticPlanner, PlannerStep
        planner = AgenticPlanner(
            workers=self._workers,
            llm_fn=self._llm_fn,
            budget=self._budget,
            fallback_playbook=self._fallback_playbook,
            max_iterations=1,  # we drive the outer loop
        )

        nudge_context: str | None = None
        prev_worker_action: tuple[str, str] | None = None
        stagnant_rounds = 0

        for iteration in range(self._max_iterations):
            if not self._budget.can_call():
                break

            # Think — with optional nudge injected into evidence
            ev_with_nudge = dict(evidence)
            if nudge_context:
                ev_with_nudge["_loop_nudge"] = nudge_context

            step = planner._think(incident, incident_type, ev_with_nudge, iteration)
            if step is None:
                step = planner._fallback_step(incident_type, evidence)
            if step is None or step.done:
                break

            # Stagnation detection
            this_call = (step.worker, step.action)
            if this_call == prev_worker_action:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
                nudge_context = None
            prev_worker_action = this_call

            if stagnant_rounds >= 2:
                telemetry.stagnation_detected = True
                if telemetry.nudge_count < LOOP_MAX_NUDGES:
                    nudge_context = self._build_nudge(step, evidence, iteration)
                    telemetry.nudge_count += 1
                    stagnant_rounds = 0
                    logger.info("Loop nudge #%d at iteration %d", telemetry.nudge_count, iteration)
                else:
                    logger.info("Loop controller: max nudges reached, breaking at iteration %d", iteration)
                    break

            # Act
            result = planner._act(step)
            evidence = planner._observe(step, result, evidence)

            # Score
            quality = self._scorer.score(incident_type, evidence)
            telemetry.quality_per_iter.append(quality)
            telemetry.iteration_records.append(
                IterationRecord(iteration, step.worker, step.action, quality, nudge_sent=bool(nudge_context))
            )
            telemetry.iterations_run = iteration + 1

            if quality >= self._convergence_threshold and telemetry.convergence_iter is None:
                telemetry.convergence_iter = iteration
                logger.info(
                    "Loop converged at iteration %d (quality=%.3f >= threshold=%.3f)",
                    iteration, quality, self._convergence_threshold,
                )
                # Don't break immediately — let one more pass run to consolidate
                if iteration > 0:
                    break

        telemetry.elapsed_ms = (time.monotonic() - t0) * 1000
        telemetry.final_quality = telemetry.quality_per_iter[-1] if telemetry.quality_per_iter else 0.0

        # Attach planner trace for audit
        evidence["_loop_telemetry"] = telemetry.to_dict()

        # Persist telemetry for API
        with _store_lock:
            _telemetry_store[telemetry.investigation_id] = telemetry
            # Cap store at 200 entries
            if len(_telemetry_store) > 200:
                oldest = next(iter(_telemetry_store))
                del _telemetry_store[oldest]

        return evidence, telemetry

    def _seed_from_memory(
        self, incident: dict, incident_type: str, seed: dict
    ) -> dict:
        """Pull top-3 similar past episodes and inject their root causes."""
        try:
            from intelligence.episodic_memory import EpisodicMemory
            mem = EpisodicMemory()
            summary = incident.get("summary", "")
            episodes = mem.search(
                incident_type=incident_type,
                failure_signature=summary[:200] if summary else None,
                limit=3,
            )
            if not episodes:
                return dict(seed)
            prior_causes = [ep.root_cause for ep in episodes if ep.root_cause]
            if prior_causes:
                seeded = dict(seed)
                seeded["_prior_root_causes"] = prior_causes
                return seeded
        except Exception as exc:
            logger.debug("Pre-seeding failed (non-critical): %s", exc)
        return dict(seed)

    def _build_nudge(self, last_step: Any, evidence: dict, iteration: int) -> str:
        """Build a redirect hint for the next Think prompt."""
        used_workers = {k.split("_")[0] for k in evidence if not k.startswith("_")}
        available_workers = set(self._workers.keys()) - used_workers
        suggest = next(iter(available_workers), "a different worker")
        return (
            f"LOOP NUDGE (iteration {iteration}): The investigation has called "
            f"{last_step.worker}.{last_step.action} repeatedly without new findings. "
            f"Do NOT call {last_step.worker}.{last_step.action} again. "
            f"Try {suggest} or set done=true if evidence is sufficient."
        )
