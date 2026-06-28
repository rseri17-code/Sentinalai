"""Workflow middleware — durability wrapper around SentinalAISupervisor.

WorkflowAwareInvestigator wraps the existing investigate() call with
start / checkpoint / complete / fail bookends backed by WorkflowEngine.

Contract:
- Does NOT modify SentinalAISupervisor or agent.py in any way.
- Does NOT change investigation output format.
- Does NOT alter worker order or playbook behavior.
- Adds SQLite durability around the existing investigate() boundary.

Integration pattern:

    from supervisor.workflow_middleware import WorkflowAwareInvestigator
    from supervisor.agent import SentinalAISupervisor

    investigator = WorkflowAwareInvestigator(SentinalAISupervisor())
    result = investigator.investigate("INC12345")

Resume behavior:
  - If a previous run completed: returns the persisted result immediately
    without re-running (idempotent).
  - If a previous run is RUNNING (orphaned after crash): logs a warning,
    starts a fresh run marked as RESUMED, and runs the investigation again.
  - If no prior run: starts fresh.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from supervisor.workflow_engine import WorkflowEngine, get_engine
from sentinel_core.models.workflow import WorkflowStatus
from sentinel_core.context import InvestigationContext

logger = logging.getLogger("sentinalai.workflow_middleware")


class WorkflowAwareInvestigator:
    """Thin durability wrapper around a SentinalAISupervisor instance.

    Only the constructor and investigate() method are exposed — the same
    interface callers already use.
    """

    def __init__(
        self,
        supervisor: Any,
        engine: Optional[WorkflowEngine] = None,
    ) -> None:
        self._supervisor = supervisor
        self._engine = engine or get_engine()

    def investigate(self, incident_id: str, replay: bool = False) -> dict[str, Any]:
        """Run or resume a durable investigation.

        Delegates to the underlying supervisor.investigate() unmodified.
        Bookends the call with workflow lifecycle events persisted to SQLite.
        """
        investigation_id = f"inv-{incident_id}"

        # Check for a prior run
        checkpoint = self._engine.resume(investigation_id)
        if checkpoint is not None:
            if checkpoint.status == WorkflowStatus.COMPLETED:
                cached = checkpoint.result_snapshot
                if cached:
                    logger.info(
                        "workflow: returning cached result for completed %s", investigation_id
                    )
                    return cached
                # Result too large to have been cached — re-run anyway
                logger.info(
                    "workflow: %s already completed (no cached result), re-running",
                    investigation_id,
                )

            elif checkpoint.status == WorkflowStatus.RUNNING:
                logger.warning(
                    "workflow: %s was RUNNING but no completion recorded — "
                    "likely orphaned after crash; starting fresh run",
                    investigation_id,
                )
                # Mark the orphaned run as RESUMED so the timeline reflects it
                self._engine.fail(
                    investigation_id,
                    "orphaned: detected on restart, superseded by new run",
                )
                # Use a unique investigation_id for the fresh run so history is preserved
                investigation_id = f"{investigation_id}-r{int(time.time())}"

        # Register the new run
        self._engine.start(investigation_id, metadata={"incident_id": incident_id})

        try:
            result = self._supervisor.investigate(incident_id, replay=replay)
            self._engine.complete(
                investigation_id,
                result_summary=self._safe_result_summary(result),
            )
            return result

        except Exception as exc:
            self._engine.fail(investigation_id, str(exc))
            raise

    # ------------------------------------------------------------------
    # Context-aware adoption (Phase 6 — additive)
    # ------------------------------------------------------------------

    def investigate_with_context(
        self,
        ctx: InvestigationContext,
        replay: bool = False,
    ) -> dict[str, Any]:
        """Run a durable investigation driven by an InvestigationContext.

        Thin convenience wrapper: forwards ``ctx.incident_id`` to the existing
        ``investigate()`` path so semantics are identical. The context's
        ``investigation_id`` and ``to_workflow_metadata()`` are NOT consulted
        here — the existing path derives ``inv-{incident_id}`` itself — which
        keeps this method behavior-equivalent to ``investigate(incident_id)``.
        """
        return self.investigate(ctx.incident_id, replay=replay)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_result_summary(result: dict[str, Any]) -> dict[str, Any]:
        """Extract a compact, storable summary of the investigation result."""
        if not isinstance(result, dict):
            return {}
        return {
            "incident_id":      result.get("incident_id", ""),
            "root_cause":       str(result.get("root_cause", ""))[:500],
            "confidence":       result.get("confidence", 0),
            "reasoning":        str(result.get("reasoning", ""))[:1000],
            "stop_reason":      result.get("stop_reason", ""),
            "degraded":         result.get("degraded", False),
            "hypothesis_count": result.get("_hypothesis_count", 0),
        }
