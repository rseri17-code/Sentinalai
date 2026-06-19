"""Agentic planner: Think→Act→Observe loop for dynamic tool selection.

Replaces the fixed playbook when AGENTIC_PLANNER=true. The LLM reasons
about which tool to call next based on current evidence, rather than
following a predetermined step sequence.

Gated by AGENTIC_PLANNER env var (default: false).
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger(__name__)

MAX_PLANNER_ITERATIONS = int(os.environ.get("PLANNER_MAX_ITERATIONS", "10"))


@dataclass
class PlannerStep:
    worker: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    done: bool = False


@dataclass
class PlannerTrace:
    """Audit trail of what the planner decided."""
    steps: list[dict] = field(default_factory=list)
    iterations: int = 0
    fallback_used: bool = False

    def record(self, step: PlannerStep, result_summary: str) -> None:
        self.steps.append({
            "iteration": self.iterations,
            "worker": step.worker,
            "action": step.action,
            "rationale": step.rationale,
            "result_summary": result_summary[:200],
            "done": step.done,
        })


class AgenticPlanner:
    """Think→Act→Observe loop that dynamically selects tools per iteration."""

    def __init__(
        self,
        workers: dict[str, Any],
        llm_fn: Callable,          # converse() from supervisor.llm
        budget: Any,               # ExecutionBudget instance
        fallback_playbook: list[dict] | None = None,
        max_iterations: int = MAX_PLANNER_ITERATIONS,
    ) -> None:
        self._workers = workers
        self._llm_fn = llm_fn
        self._budget = budget
        self._fallback_playbook = fallback_playbook or []
        self._max_iterations = max_iterations
        self._trace = PlannerTrace()

    def run(
        self,
        incident_id: str,
        incident: dict,
        incident_type: str,
        seed_evidence: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], PlannerTrace]:
        """Run the Think→Act→Observe loop.

        Returns (accumulated_evidence, trace).
        """
        evidence: dict[str, Any] = dict(seed_evidence or {})

        for iteration in range(self._max_iterations):
            self._trace.iterations = iteration + 1

            if not self._budget.can_call():
                logger.info("Planner: budget exhausted after %d iterations", iteration)
                break

            # Think
            step = self._think(incident, incident_type, evidence, iteration)

            if step is None:
                logger.info("Planner: LLM failed to produce a valid step — using fallback")
                step = self._fallback_step(incident_type, evidence)
                self._trace.fallback_used = True

            if step is None or step.done:
                logger.info("Planner: done signal at iteration %d", iteration)
                break

            # Act
            result = self._act(step)

            # Observe
            evidence = self._observe(step, result, evidence)
            self._trace.record(step, self._summarize_result(result))

        # Attach trace for auditability
        evidence["_planner_trace"] = {
            "iterations": self._trace.iterations,
            "steps": self._trace.steps,
            "fallback_used": self._trace.fallback_used,
        }
        return evidence, self._trace

    def _think(
        self, incident: dict, incident_type: str, evidence: dict, iteration: int
    ) -> PlannerStep | None:
        """Call LLM to decide the next tool."""
        prompt = self._build_prompt(incident, incident_type, evidence, iteration)
        try:
            response = self._llm_fn(
                system_prompt=(
                    "You are an SRE investigation planner. Given an incident and evidence "
                    "collected so far, decide the single most valuable next tool call. "
                    "Respond with ONLY valid JSON matching the schema provided."
                ),
                user_message=prompt,
                temperature=0.0,
                max_tokens=300,
            )
            if response.get("error"):
                return None
            return self._parse_response(response.get("text", ""))
        except Exception as exc:
            logger.debug("Planner think() failed: %s", exc)
            return None

    def _act(self, step: PlannerStep) -> dict:
        """Execute the chosen tool."""
        worker = self._workers.get(step.worker)
        if worker is None:
            logger.warning("Planner: worker %s not available", step.worker)
            return {"error": f"worker_not_found: {step.worker}"}
        try:
            return worker.execute(step.action, step.params) or {}
        except Exception as exc:
            logger.warning("Planner: action %s.%s failed: %s", step.worker, step.action, exc)
            return {"error": str(exc)}

    def _observe(self, step: PlannerStep, result: dict, evidence: dict) -> dict:
        """Merge result into evidence dict."""
        updated = dict(evidence)
        label = f"{step.worker}_{step.action}"
        if result and not result.get("error"):
            updated[label] = result
            # Also merge top-level keys if result is rich
            for k, v in result.items():
                if k not in updated and not k.startswith("_"):
                    updated[k] = v
        return updated

    def _build_prompt(
        self, incident: dict, incident_type: str, evidence: dict, iteration: int
    ) -> str:
        """Build the Think prompt."""
        available = [
            f"{name}.{action}"
            for name, worker in self._workers.items()
            for action in (worker._handlers.keys() if hasattr(worker, "_handlers") else [])
        ]
        evidence_summary = ", ".join(
            k for k in evidence.keys() if not k.startswith("_")
        ) or "none yet"

        return f"""Incident: {incident.get('summary', '')[:300]}
Type: {incident_type}
Service: {incident.get('affected_service', 'unknown')}
Evidence collected so far: {evidence_summary}
Iteration: {iteration + 1} of {self._max_iterations}
Budget remaining: {getattr(self._budget, 'remaining', lambda: '?')()}

Available tools: {', '.join(available[:20])}

Respond with JSON only:
{{"worker": "<worker_name>", "action": "<action_name>", "params": {{}}, "rationale": "<one sentence>", "done": false}}

Set done=true if you have enough evidence to determine root cause."""

    def _parse_response(self, text: str) -> PlannerStep | None:
        """Parse LLM JSON response into PlannerStep."""
        # Strip markdown fences if present
        text = text.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        try:
            data = json.loads(text)
            return PlannerStep(
                worker=str(data.get("worker", "")),
                action=str(data.get("action", "")),
                params=data.get("params", {}),
                rationale=str(data.get("rationale", "")),
                done=bool(data.get("done", False)),
            )
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.debug("Planner: failed to parse LLM response: %s\nText: %s", exc, text[:200])
            return None

    def _fallback_step(self, incident_type: str, evidence: dict) -> PlannerStep | None:
        """Return the next unexecuted step from the fallback playbook."""
        executed_labels = {
            f"{s.get('worker')}_{s.get('action')}"
            for s in self._trace.steps
        }
        for step in self._fallback_playbook:
            label = f"{step.get('worker')}_{step.get('action')}"
            if label not in executed_labels:
                return PlannerStep(
                    worker=step.get("worker", ""),
                    action=step.get("action", ""),
                    params={},
                    rationale="fallback playbook step",
                )
        return PlannerStep(worker="", action="", done=True)

    @staticmethod
    def _summarize_result(result: dict) -> str:
        if not result:
            return "empty"
        if result.get("error"):
            return f"error: {result['error']}"
        keys = [k for k in result if not k.startswith("_")]
        return f"keys: {keys[:5]}"
